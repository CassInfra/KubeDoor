"""
🔄 K8S节点负载均衡调度服务
负责配置管理、定时调度、迁移清单缓存、操作日志
"""

import json
import asyncio
from datetime import datetime
from typing import Optional
from pathlib import Path
from loguru import logger

# 默认配置
DEFAULT_CONFIG = {
    "enabled": False,
    "imbalance_threshold": 30,  # 不均衡阈值(%)
    "balance_target": 20,       # 均衡目标(%)
    "check_interval": 5,        # 检测间隔(分钟)
    "min_replicas": 2,          # 最小副本数
    "peak_hours": [],           # 高峰期时间段 [{"start": "09:00", "end": "12:00"}, ...]
    "blacklist": [],            # 黑名单
    "exclude_namespaces": ["kube-system", "kubedoor", "istio-system"],
    "exclude_nodes": [],        # 排除节点（始终保持cordon，不参与调度）
    "auto_execute": False,      # 是否自动执行
    # 节点选择策略: "average"=平均值, "percentile"=分位数
    "source_strategy": "average",   # 源节点选择策略
    "source_percentile": 70,        # 源节点分位数阈值（高于此分位数）
    "target_strategy": "average",   # 目标节点选择策略
    "target_percentile": 30,        # 目标节点分位数阈值（低于此分位数）
    "min_pod_cpu": 0.5,             # 最小Pod CPU阈值（核），低于此值的Pod不参与迁移
    "max_iterations": 50,           # 最大迭代次数
    "batch_size": 4                 # 每批执行数量
}

# 配置文件路径
CONFIG_FILE = Path(__file__).parent / "config.json"


class LoadBalanceService:
    """负载均衡调度服务"""

    _instance: Optional["LoadBalanceService"] = None

    def __init__(self):
        self.config = DEFAULT_CONFIG.copy()
        self.pending_plan: Optional[dict] = None  # 待确认的迁移清单
        self.logs: list[dict] = []  # 操作日志（内存中保留最近100条）
        self.status = "idle"  # idle, analyzing, executing
        self.last_check_time: Optional[datetime] = None
        self._load_config()

    @classmethod
    def get_instance(cls) -> "LoadBalanceService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _load_config(self):
        """加载配置文件"""
        try:
            if CONFIG_FILE.exists():
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                    self.config.update(saved)
                logger.info(f"📂 已加载负载均衡配置: {CONFIG_FILE}")
        except Exception as e:
            logger.warning(f"⚠️ 加载配置失败，使用默认配置: {e}")

    def _save_config(self):
        """保存配置文件"""
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
            logger.info(f"💾 已保存负载均衡配置")
        except Exception as e:
            logger.error(f"❌ 保存配置失败: {e}")

    def get_config(self) -> dict:
        """获取配置"""
        return self.config.copy()

    def update_config(self, new_config: dict) -> dict:
        """更新配置"""
        # 只更新允许的字段
        allowed_keys = set(DEFAULT_CONFIG.keys())
        for key, value in new_config.items():
            if key in allowed_keys:
                self.config[key] = value

        self._save_config()
        self._add_log("config_update", f"配置已更新")
        return self.config.copy()

    def get_status(self) -> dict:
        """获取当前状态"""
        return {
            "status": self.status,
            "enabled": self.config.get("enabled", False),
            "last_check_time": self.last_check_time.isoformat() if self.last_check_time else None,
            "has_pending_plan": self.pending_plan is not None,
            "pending_migrations_count": len(self.pending_plan.get("migrations", [])) if self.pending_plan else 0
        }

    def get_pending_plan(self) -> Optional[dict]:
        """获取待确认的迁移清单"""
        return self.pending_plan

    def set_pending_plan(self, plan: dict):
        """设置待确认的迁移清单"""
        self.pending_plan = plan
        self._add_log("plan_generated", f"生成迁移清单: {len(plan.get('migrations', []))} 个Pod")

    def clear_pending_plan(self):
        """清除待确认的迁移清单"""
        self.pending_plan = None

    def get_logs(self, limit: int = 50) -> list[dict]:
        """获取操作日志"""
        return self.logs[-limit:]

    def _add_log(self, action: str, message: str, details: dict = None):
        """添加操作日志"""
        log = {
            "timestamp": datetime.now().isoformat(),
            "action": action,
            "message": message,
            "details": details
        }
        self.logs.append(log)
        # 保留最近100条
        if len(self.logs) > 100:
            self.logs = self.logs[-100:]

    def is_peak_hour(self) -> bool:
        """检查当前是否在高峰期"""
        if not self.config.get("peak_hours"):
            return False

        now = datetime.now()
        current_time = now.strftime("%H:%M")

        for period in self.config["peak_hours"]:
            start = period.get("start", "00:00")
            end = period.get("end", "23:59")
            if start <= current_time <= end:
                return True

        return False

    def should_check(self) -> bool:
        """检查是否应该执行检测"""
        if not self.config.get("enabled"):
            return False

        # 检查是否在高峰期
        if self.config.get("peak_hours") and not self.is_peak_hour():
            return False

        # 检查间隔
        if self.last_check_time:
            interval = self.config.get("check_interval", 5)
            elapsed = (datetime.now() - self.last_check_time).total_seconds() / 60
            if elapsed < interval:
                return False

        return True

    def mark_check_done(self):
        """标记检测完成"""
        self.last_check_time = datetime.now()

    def log_analyze_start(self):
        """记录分析开始"""
        self.status = "analyzing"
        self._add_log("analyze_start", "开始分析负载")

    def log_analyze_done(self, plan: dict):
        """记录分析完成"""
        self.status = "idle"
        migrations = plan.get("migrations", [])
        if migrations:
            self._add_log("analyze_done", f"分析完成，生成 {len(migrations)} 个迁移计划",
                         {"current_range": plan.get("current_range"),
                          "expected_range": plan.get("expected_range")})
        else:
            self._add_log("analyze_done", f"分析完成，无需迁移: {plan.get('message', '')}")

    def log_execute_start(self, count: int):
        """记录执行开始"""
        self.status = "executing"
        self._add_log("execute_start", f"开始执行迁移，共 {count} 个Pod")

    def log_execute_done(self, result: dict):
        """记录执行完成"""
        self.status = "idle"
        self._add_log("execute_done",
                     f"迁移完成: {result.get('success', 0)}/{result.get('total', 0)} 成功",
                     result)

    def log_cleanup(self, result: dict):
        """记录清理操作"""
        self._add_log("cleanup", f"清理隔离Pod: {len(result.get('success', []))} 个成功", result)


def get_service() -> LoadBalanceService:
    """获取服务单例"""
    return LoadBalanceService.get_instance()
