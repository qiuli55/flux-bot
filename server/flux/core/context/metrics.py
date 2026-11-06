"""投喂度量（FLUX_CONTEXT_MECHANISM §5.6）。

§5.6 列了六个指标，其中两个是**硬红线**，其余是"越省越好"的观测值：

| 指标 | 目标 |
| --- | --- |
| 压缩率 | —（投喂体积 / 原文体积） |
| 必留项命中率 | 100%（永不压缩清单一条不漏） |
| 重建一致率 | 恒 100%（S5 校验通过率，非 100% 即 bug） |
| fetch 回补率 | 越低越好（运行时观测，见 §6.7 / C4） |
| 目录行长度 | 受控（§6.4，在目录渲染处量） |
| 去重命中率 | 越高越省（§3.5 内容寻址的实际收益） |

本模块接的是前三个**能在打包时算出来**的：调用方只报原始计数，换算与红线判定收在
一处，免得"压缩率"在打包器、观测端、测试里各算一遍除法而口径漂移。

后三个里，目录行长度在 `catalog` 量、去重命中率在内容寻址处量；**回补率**（§6.7）
由本模块的 `BackfillObserver` 运行时累积：打包时记 offer、取回时记 fetch。§6.7 说
回补是**度量信号**——回补率持续偏高即说明压缩过头，应回退装配等级，故这里也给出
`should_rollback` 这个现成的判据，免得阈值在别处再拍一遍。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FeedMetrics:
    """一次投喂的原始计数（字段单位一律为字符数，与 §6.1 的预算同口径）。"""

    source_chars: int
    fed_chars: int
    required_total: int
    required_kept: int
    verified: int
    verified_ok: int
    dropped_count: int

    @property
    def compression_ratio(self) -> float:
        """投喂体积 / 原文体积；原文为空时记 1.0（没压也没胀）。"""
        if self.source_chars <= 0:
            return 1.0
        return self.fed_chars / self.source_chars

    @property
    def required_hit_rate(self) -> float:
        """必留项命中率：永不压缩清单一条不漏（§5.4）。"""
        if self.required_total <= 0:
            return 1.0
        return self.required_kept / self.required_total

    @property
    def reconstruction_rate(self) -> float:
        """重建一致率：S5 校验通过率，非 100% 即 bug（§5.6）。"""
        if self.verified <= 0:
            return 1.0
        return self.verified_ok / self.verified

    @property
    def healthy(self) -> bool:
        """两条硬红线都达标：必留项一条不漏、S5 一次不差。"""
        return self.required_hit_rate >= 1.0 and self.reconstruction_rate >= 1.0

    def as_dict(self) -> dict[str, float | int | bool]:
        """拍平成可直接塞进 `context.get` 返回值 / 日志的观测字段。"""
        return {
            "source_chars": self.source_chars,
            "fed_chars": self.fed_chars,
            "compression_ratio": round(self.compression_ratio, 4),
            "required_total": self.required_total,
            "required_kept": self.required_kept,
            "required_hit_rate": round(self.required_hit_rate, 4),
            "verified": self.verified,
            "verified_ok": self.verified_ok,
            "reconstruction_rate": round(self.reconstruction_rate, 4),
            "dropped_count": self.dropped_count,
            "healthy": self.healthy,
        }


@dataclass(frozen=True)
class BackfillPolicy:
    """回补率的判据阈值（§6.7 只给语义，数值在此定死，便于整体替换与测试）。"""

    #: 回补率上限：超过它即认为压缩过头
    max_rate: float = 0.25
    #: 最小样本数：offer 太少时不给结论，免得刚开工就被一两次偶然取回触发回退
    min_samples: int = 20


DEFAULT_BACKFILL_POLICY = BackfillPolicy()


@dataclass(frozen=True)
class BackfillStats:
    """回补的原始计数与派生指标（§6.7）。

    `offered` 是打包器给出去的引用数（被裁掉的条目），`fetched` 是 agent 真的按需取回
    的次数。回补率 = 取回 / 给出去，**越低越好**：该省的历史省掉了才算压得准。
    """

    offered: int = 0
    fetched: int = 0

    @property
    def rate(self) -> float:
        """回补率 = 取回 / 给出去的引用数；一条都没给出去时记 0.0（无从回补）。"""
        if self.offered <= 0:
            return 0.0
        return self.fetched / self.offered

    def should_rollback(self, policy: BackfillPolicy = DEFAULT_BACKFILL_POLICY) -> bool:
        """是否应回退装配等级（§6.7）：样本够多且回补率超限，才是真的压过头。"""
        return self.offered >= policy.min_samples and self.rate > policy.max_rate

    def as_dict(
        self, policy: BackfillPolicy = DEFAULT_BACKFILL_POLICY
    ) -> dict[str, float | int | bool]:
        """拍平成可直接塞进观测字段的形态：计数 + 比率 + 回退判据。"""
        return {
            "offered": self.offered,
            "fetched": self.fetched,
            "rate": round(self.rate, 4),
            "rollback": self.should_rollback(policy),
        }


class BackfillObserver:
    """回补率的运行时累积器（§6.7）：打包时 `note_offered`、取回时 `note_fetched`。

    这是进程内的观测账：只数次数、不记内容，够把"压过头了"这个信号报出来即可。
    """

    def __init__(self, policy: BackfillPolicy = DEFAULT_BACKFILL_POLICY) -> None:
        self._policy = policy
        self._offered = 0
        self._fetched = 0

    def note_offered(self, count: int) -> None:
        """记一次打包给出去多少条引用（负数按 0 计，防调用方算错把账做成负的）。"""
        self._offered += max(0, count)

    def note_fetched(self) -> None:
        """记一次成功的按需取回（`context.fetch` 真的把原文交回给 agent）。"""
        self._fetched += 1

    def stats(self) -> BackfillStats:
        return BackfillStats(offered=self._offered, fetched=self._fetched)

    def should_rollback(self) -> bool:
        return self.stats().should_rollback(self._policy)

    def as_dict(self) -> dict[str, float | int | bool]:
        return self.stats().as_dict(self._policy)

    def reset(self) -> None:
        """清零观测账（换会话 / 换观测窗口时用）。"""
        self._offered = 0
        self._fetched = 0
