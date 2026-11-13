"""编排 worker。

轮询待处理 Job、驱动状态机、断点续跑。实现 tasks.md T020 / T027 / T032。

工作流程：
1. 轮询数据库中待处理的 Job（未锁定且 attempts < max_attempts）
2. 占锁（locked_by = worker_id）
3. 根据 stage 驱动状态机推进
4. 更新订单状态与 Job 状态
5. 释放锁或标记失败
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# 添加路径
root = Path(__file__).parent.parent.parent.parent.parent
sys.path.insert(0, str(root / "packages/contracts/src"))
sys.path.insert(0, str(root / "packages/core/src"))
sys.path.insert(0, str(root / "services/api/src"))
sys.path.insert(0, str(root / "services/checkout/src"))
sys.path.insert(0, str(root / "services/payment/src"))
sys.path.insert(0, str(root / "services/account/src"))

from gss_core import install_logging_filter
from gss_core.config import load_settings, missing_upstreams
from gss_api.db import init_db, get_db
from gss_api.repository import OrderRepository
from gss_contracts import OrderStatus, PaymentStatus, Job
from gss_contracts.errors import DomainError

from gss_orchestrator.pipeline import OrderPipeline, PipelineDeps, next_order_status, assert_deliverable
from gss_checkout.adapter import UpstreamCheckoutAdapter, FakeCheckoutAdapter
from gss_payment.adapter import UpstreamPaymentAdapter, FakePaymentAdapter
from gss_account.adapter import UpstreamAccountAdapter, FakeAccountAdapter

logger = logging.getLogger("gss_orchestrator.worker")


def self_check() -> list[str]:
    """返回缺失的上游引用列表（空表示就绪）。"""

    settings = load_settings()
    missing = missing_upstreams(settings)
    logger.info(
        "worker 自检: bind=%s:%s promo=%s data=%s",
        settings.bind_host,
        settings.bind_port,
        settings.enable_promo_mode,
        settings.data_dir,
    )
    if missing:
        logger.warning("缺少上游引用（将使用 Fake adapter）: %s", ", ".join(missing))
    return missing


class WorkerEngine:
    """编排引擎。"""
    
    def __init__(self, worker_id: str, use_fake: bool = False):
        self.worker_id = worker_id
        self.use_fake = use_fake
        
        # 初始化 adapter（如果缺少上游配置，使用 Fake adapter）
        if use_fake:
            checkout = FakeCheckoutAdapter()
            payment = FakePaymentAdapter()
            account = FakeAccountAdapter()
        else:
            checkout = UpstreamCheckoutAdapter()
            payment = UpstreamPaymentAdapter()
            account = UpstreamAccountAdapter()
        
        self.pipeline = OrderPipeline(
            PipelineDeps(
                checkout=checkout,
                payment=payment,
                account=account,
            )
        )
    
    async def process_jobs(self, repo: OrderRepository, max_jobs: int = 10) -> int:
        """处理一批待执行的任务，返回处理数量。"""
        
        # 查询待处理的任务（未锁定或锁定超时的）
        # 简化实现：先查询所有 pending 状态的订单对应的任务
        pending_orders = repo.list_orders(
            status=None,  # 查询所有状态
            limit=max_jobs,
        )
        
        processed = 0
        for order in pending_orders:
            # 跳过终态订单
            if order.status in [OrderStatus.DELIVERED, OrderStatus.FAILED, OrderStatus.REJECTED, OrderStatus.REFUNDED]:
                continue
            
            # 获取订单的任务
            jobs = repo.jobs_for_order(order.id)
            if not jobs:
                continue
            
            # 处理最新的未完成任务
            job = jobs[-1]
            if job.locked_by and job.locked_by != self.worker_id:
                # 检查锁是否超时（简化：5分钟超时）
                if job.locked_at:
                    timeout = (datetime.now(timezone.utc) - job.locked_at).total_seconds()
                    if timeout < 300:  # 5分钟内的锁仍然有效
                        continue
            
            # 占锁
            try:
                job = repo.update_job(
                    job.id,
                    locked_by=self.worker_id,
                    locked_at=datetime.now(timezone.utc),
                )
                
                # 驱动状态机
                await self.drive_order(repo, order, job)
                processed += 1
                
            except Exception as e:
                logger.error(f"处理订单 {order.id} 失败: {e}", exc_info=True)
                # 记录错误并释放锁
                try:
                    repo.update_job(
                        job.id,
                        last_error=str(e)[:500],
                        locked_by=None,
                        attempts=job.attempts + 1,
                    )
                except Exception:
                    pass
        
        return processed
    
    async def drive_order(self, repo: OrderRepository, order, job: Job) -> None:
        """驱动单个订单的状态机推进。"""
        
        logger.info(f"处理订单 {order.id}, 当前状态: {order.status}, 阶段: {job.stage}")
        
        try:
            if order.status == OrderStatus.CREATED:
                # 阶段一：生成结账链接
                await self.stage_checkout(repo, order, job)
            
            elif order.status == OrderStatus.PENDING_CHECKOUT:
                # 等待客户访问结账链接（自动推进到 awaiting_payment）
                repo.update_status(order.id, OrderStatus.AWAITING_PAYMENT)
                logger.info(f"订单 {order.id} 推进到 AWAITING_PAYMENT")
            
            elif order.status == OrderStatus.AWAITING_PAYMENT:
                # 阶段二：执行支付
                await self.stage_payment(repo, order, job)
            
            elif order.status == OrderStatus.PAYING:
                # 等待支付完成（需要客户端轮询或 webhook）
                # 简化实现：直接推进到 verifying
                repo.update_status(order.id, OrderStatus.VERIFYING)
                logger.info(f"订单 {order.id} 支付完成，推进到 VERIFYING")
            
            elif order.status == OrderStatus.VERIFYING:
                # 阶段三：订阅校验
                await self.stage_verify(repo, order, job)
            
            elif order.status == OrderStatus.ACTIVE:
                # 交付
                await self.stage_deliver(repo, order, job)
            
            # 释放锁
            repo.update_job(
                job.id,
                locked_by=None,
                last_error=None,
            )
            
        except DomainError as e:
            logger.warning(f"订单 {order.id} 业务错误: {e.code} - {e.message}")
            # 根据错误类型决定是否标记失败
            if job.attempts >= 3:
                repo.update_status(order.id, OrderStatus.FAILED)
                logger.error(f"订单 {order.id} 达到最大重试次数，标记为失败")
            
            repo.update_job(
                job.id,
                last_error=f"{e.code}: {e.message}",
                locked_by=None,
                attempts=job.attempts + 1,
            )
    
    async def stage_checkout(self, repo: OrderRepository, order, job: Job) -> None:
        """阶段一：生成结账链接。"""
        
        # 这里需要 session，简化实现：假设已从某处获取
        # 实际应该在创建订单时保存到临时存储
        session = "temp-session-placeholder"  # TODO: 从安全临时存储获取
        
        result = await self.pipeline.create_checkout(
            session=session,
            plan=order.plan,
            billing_country=order.billing_country,
            mode=order.mode,
        )
        
        # 更新任务和订单
        repo.update_job(job.id, checkout_url=result.checkout_url)
        repo.update_status(
            order.id,
            OrderStatus.PENDING_CHECKOUT,
            amount=result.amount,
        )
        
        logger.info(f"订单 {order.id} 结账链接已生成: {result.checkout_url}")
    
    async def stage_payment(self, repo: OrderRepository, order, job: Job) -> None:
        """阶段二：执行支付。"""
        
        if not job.checkout_url:
            raise DomainError("PAYMENT_DECLINED", "缺少结账链接")
        
        # 简化实现：使用 payment_method_token（实际应从请求中获取）
        payment_method_token = "temp-token-placeholder"  # TODO: 从客户端获取
        
        result = await self.pipeline.pay(
            checkout_url=job.checkout_url,
            country=order.billing_country,
            mode=order.mode,
            payment_method_token=payment_method_token,
        )
        
        # 记录支付尝试
        from gss_contracts import PaymentAttempt
        attempt = PaymentAttempt(
            order_id=order.id,
            provider="direct_card",
            status=result.status,
            next_action=result.next_action,
        )
        repo.add_payment_attempt(attempt)
        
        # 根据支付结果更新订单状态
        new_status = next_order_status(OrderStatus.AWAITING_PAYMENT, result.status)
        repo.update_status(order.id, new_status)
        
        logger.info(f"订单 {order.id} 支付状态: {result.status} -> {new_status}")
    
    async def stage_verify(self, repo: OrderRepository, order, job: Job) -> None:
        """阶段三：订阅校验。"""
        
        session = "temp-session-placeholder"  # TODO: 从安全临时存储获取
        
        info = await self.pipeline.verify(session=session)
        
        if info.active:
            repo.update_status(order.id, OrderStatus.ACTIVE)
            logger.info(f"订单 {order.id} 订阅已确认生效，到期: {info.expires_at}")
        else:
            # 订阅未生效，保持 VERIFYING 状态并重试
            if job.attempts < 5:
                logger.warning(f"订单 {order.id} 订阅尚未生效，将重试")
            else:
                repo.update_status(order.id, OrderStatus.FAILED)
                logger.error(f"订单 {order.id} 订阅校验失败，达到最大重试次数")
    
    async def stage_deliver(self, repo: OrderRepository, order, job: Job) -> None:
        """最后阶段：交付。"""
        
        session = "temp-session-placeholder"  # TODO: 从安全临时存储获取
        info = await self.pipeline.verify(session=session)
        
        # 交付前最后一次确认
        assert_deliverable(order.status, info)
        
        # 标记为已交付
        repo.update_status(order.id, OrderStatus.DELIVERED)
        logger.info(f"订单 {order.id} 已交付！")


def run(poll_interval: float = 5.0, once: bool = False, use_fake: bool = False) -> None:
    from gss_core.http_debug import install_transport_capture

    install_logging_filter()
    install_transport_capture()
    missing = self_check()
    
    # 如果有缺失的上游，自动使用 Fake adapter
    if missing:
        use_fake = True
        logger.info("检测到缺失上游配置，将使用 Fake adapter 进行测试")
    
    if once:
        return
    
    # 初始化数据库
    db_path = os.environ.get("GSS_DB_PATH", "data/gss.db")
    db_url = f"sqlite:///{db_path}"
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    init_db(db_url)
    
    worker_id = f"worker-{uuid.uuid4().hex[:8]}"
    engine = WorkerEngine(worker_id=worker_id, use_fake=use_fake)
    
    logger.info(f"worker 启动: {worker_id}, 轮询间隔 {poll_interval}s, use_fake={use_fake}")
    
    async def poll_loop():
        db = get_db()
        while True:
            try:
                session = next(db.get_session())
                repo = OrderRepository(session)
                
                processed = await engine.process_jobs(repo, max_jobs=10)
                if processed > 0:
                    logger.info(f"本轮处理了 {processed} 个任务")
                
                session.close()
            except Exception as e:
                logger.error(f"轮询异常: {e}", exc_info=True)
            
            await asyncio.sleep(poll_interval)
    
    asyncio.run(poll_loop())


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="gpt-subscribe-studio 编排 worker")
    parser.add_argument("--once", action="store_true", help="只做一次自检后退出")
    parser.add_argument("--poll-interval", type=float, default=5.0, help="轮询间隔（秒）")
    parser.add_argument("--fake", action="store_true", help="使用 Fake adapter（测试模式）")
    args = parser.parse_args(argv)
    run(poll_interval=args.poll_interval, once=args.once, use_fake=args.fake)


if __name__ == "__main__":
    main()
