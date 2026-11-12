"""中国区模型供应商的官方端点与模型 id 快照（2026-11-12 核验，America/Chicago）。

只收录官方文档明确写出的值：它们既是 `Settings` 默认值的来源，也是测试断言的依据。
不收录单价——`pricing` 恒为 None，金额必须来自官方价格页快照（主规格 §15.4），
属 M6「成本与可观测」范围，本次不碰。

四条通道都兼容 OpenAI 的 `POST /chat/completions` 协议，鉴权头统一为
`Authorization: Bearer <API_KEY>`，所以适配器都只是 `OpenAICompatibleProvider`
的薄子类。工作区专属域名、地域域名（如 Qwen 新加坡）与订阅套餐端点一律不进默认值，
由 `FLUX_<PROVIDER>_BASE_URL` 或密钥库条目的 `base_url` 覆盖。
"""

from __future__ import annotations

# --- Qwen / DashScope（阿里云百炼）---
# 来源：https://www.alibabacloud.com/help/zh/model-studio/base-url
# （页面标注更新时间 2026-10-11；2026-11-12 抓取）
# 北京共享域名不需要 workspace id，作为默认值；新加坡地域为
# https://dashscope-intl.aliyuncs.com/compatible-mode/v1，需要时显式覆盖。
QWEN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
# 默认选成本优先的 flash：官方文本生成模型列表（2026-11-04 版）与
# text-generation-model 页均推荐 qwen3.8 系列。qwen4-max-preview 仅限 Token Plan、
# 不面向后端服务，故不作默认值。
QWEN_DEFAULT_MODEL = "qwen3.8-flash"
QWEN_DOCUMENTED_MODELS: tuple[str, ...] = (
    "qwen3.8-max",
    "qwen3.8-flash",
    "qwen3.7-plus",
    "qwen3.7-max",
    "qwen3.7-flash",
    "qwen-plus",
    "qwen-max",
    "qwen-flash",
)

# --- Zhipu GLM（智谱开放平台）---
# 来源：https://docs.bigmodel.cn/cn/api/introduction 与
# https://docs.bigmodel.cn/cn/guide/models/text/glm-5.3（2026-11-12 抓取）
# 注意：https://open.bigmodel.cn/api/coding/paas/v4 是 Coding 订阅套餐专用端点，
# 不能当默认值；需要时用 base_url 覆盖。
ZHIPU_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
ZHIPU_DEFAULT_MODEL = "glm-5.3"
ZHIPU_DOCUMENTED_MODELS: tuple[str, ...] = ("glm-5.3",)

# --- Kimi / Moonshot（月之暗面）---
# 来源：https://platform.kimi.ai/docs/api/overview（Last-Modified 2026-11-11）
# 与 https://platform.kimi.ai/docs/api/quickstart（2026-11-12 抓取）
# 官方当前文档用 api.moonshot.ai；部分用户可能改用 .cn 域名，故允许 base_url 覆盖。
KIMI_BASE_URL = "https://api.moonshot.ai/v1"
KIMI_DEFAULT_MODEL = "kimi-k3.1-pro"
KIMI_DOCUMENTED_MODELS: tuple[str, ...] = ("kimi-k3.1-pro", "kimi-k3")

# --- Doubao / Volcengine Ark（火山方舟）---
# 来源：https://ark.volcengine.com/region:cn-beijing/docs/compatible-with-openai-sdk
# 与 https://ark.volcengine.com/region:cn-beijing/docs/base-url-and-authentication
# （2026-11-12 抓取）
# 官方示例里的模型 id 是 doubao-seed-2-1-pro-260915。Ark 的 model 字段也接受
# 推理接入点 Endpoint ID（ep-... 形式）：那是账号内资源、无法在代码里穷举，
# 用户可用 FLUX_DOUBAO_MODEL 或密钥库条目的 models 填任意这类 id。
# 本次不实现 Access Key（AK/SK）签名，只用 Bearer API Key。
DOUBAO_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
DOUBAO_DEFAULT_MODEL = "doubao-seed-2-1-pro-260915"
DOUBAO_DOCUMENTED_MODELS: tuple[str, ...] = ("doubao-seed-2-1-pro-260915",)

__all__ = [
    "DOUBAO_BASE_URL",
    "DOUBAO_DEFAULT_MODEL",
    "DOUBAO_DOCUMENTED_MODELS",
    "KIMI_BASE_URL",
    "KIMI_DEFAULT_MODEL",
    "KIMI_DOCUMENTED_MODELS",
    "QWEN_BASE_URL",
    "QWEN_DEFAULT_MODEL",
    "QWEN_DOCUMENTED_MODELS",
    "ZHIPU_BASE_URL",
    "ZHIPU_DEFAULT_MODEL",
    "ZHIPU_DOCUMENTED_MODELS",
]
