"""设置面板背后的配置：哪些项可以改、值存在哪、怎么生效。

优先级：设置面板里保存的值（SQLite） > .env / 系统环境变量 > 默认值。
面板里清掉某一项，就回到 .env 里的值（没有就用默认值）。只有下面列出的项能改，别的环境变量碰不到。
"""
import os

# type: text / secret / select / number / toggle；secret 不会明文发给浏览器
GROUPS = [
    {"id": "models", "title": "大模型与接口", "fields": [
        {"key": "ARK_API_KEY", "label": "即梦 / 火山方舟 API Key", "type": "secret",
         "help": "只填这一个就能用换装、检查、转身视频。火山引擎控制台 → 火山方舟 → API Key 管理"},
        {"key": "SEEDREAM_MODEL", "label": "出图模型", "default": "doubao-seedream-4-5-251128",
         "help": "也可以试 doubao-seedream-5-0-260128"},
        {"key": "SEEDANCE_MODEL", "label": "转身视频模型", "default": "",
         "help": "以方舟控制台上显示的模型 ID 为准；不填会依次试 Seedance 2.0 fast / 2.0 / 1.0 系列"},
        {"key": "CHECK_MODEL", "label": "自动检查用的视觉模型", "default": "doubao-seed-1-6-250615"},
        {"key": "ARK_BASE_URL", "label": "方舟接口地址", "default": "https://ark.cn-beijing.volces.com/api/v3"},
        {"key": "DASHSCOPE_API_KEY", "label": "阿里云百炼 API Key（可选）", "type": "secret",
         "help": "对比用：开通「AI试衣-Plus」「AI试衣-图片精修」「通义千问图像编辑」"},
        {"key": "BL_TRYON_MODEL", "label": "百炼试衣模型", "default": "aitryon-plus"},
        {"key": "BL_EDIT_MODEL", "label": "百炼图像编辑模型", "default": "qwen-image-edit-plus"},
        {"key": "DASHSCOPE_BASE_URL", "label": "百炼接口地址", "default": "https://dashscope.aliyuncs.com/api/v1"},
        {"key": "KLING_ACCESS_KEY", "label": "可灵 AccessKey（可选）", "type": "secret"},
        {"key": "KLING_SECRET_KEY", "label": "可灵 SecretKey（可选）", "type": "secret"},
        {"key": "KLING_TRYON_MODEL", "label": "可灵试穿模型", "default": "kolors-virtual-try-on-v1-5"},
        {"key": "KLING_BASE_URL", "label": "可灵接口地址", "default": "https://api-beijing.klingai.com"},
        {"key": "FAL_KEY", "label": "fal.ai Key（可选）", "type": "secret"},
        {"key": "FAL_EDIT_MODEL", "label": "fal 图像编辑模型", "default": "fal-ai/nano-banana-pro/edit"},
    ]},
    {"id": "flow", "title": "换装流程", "fields": [
        {"key": "PIPELINE", "label": "换衣服用哪条流程", "type": "select", "default": "",
         "options": [["", "自动（第一个填了 key 的）"], ["seedream", "即梦"], ["bailian", "阿里云百炼"],
                     ["kling", "可灵"], ["fal", "fal.ai"]]},
        {"key": "EDIT_PROVIDER", "label": "形象照 / 换鞋 / 配饰用哪家的编辑模型", "type": "select", "default": "",
         "options": [["", "自动"], ["seedream", "即梦"], ["bailian", "阿里云百炼"], ["fal", "fal.ai"]]},
        {"key": "GENDER", "label": "默认性别", "type": "select", "default": "man",
         "options": [["man", "男"], ["woman", "女"]],
         "help": "客户档案里填了性别就按客户的；没填才用这个"},
        {"key": "AUTO_CHECK", "label": "生成后自动检查效果", "type": "toggle", "default": "1",
         "help": "不合格自动重来；需要即梦 key"},
        {"key": "AUTO_RETRY", "label": "一身最多生成几次", "type": "number", "default": "2",
         "help": "每多一次多一份生成费用"},
    ]},
    {"id": "video", "title": "转身视频", "fields": [
        {"key": "TURN_MODE", "label": "转一圈的方式", "type": "select", "default": "turntable",
         "options": [["turntable", "360° 转台（人定住整体转）"], ["orbit", "人不动，镜头环绕"]]},
        {"key": "TURN_LAST_FRAME", "label": "首尾帧", "type": "toggle", "default": "0",
         "help": "照片同时当开头和结尾；Seedance 2.0 不收真人照片做首尾帧，默认关"},
        {"key": "VIDEO_SECONDS", "label": "视频长度（秒）", "type": "number", "default": "",
         "help": "2.0 默认 6 秒，1.0 版只支持 5 或 10"},
        {"key": "VIDEO_RESOLUTION", "label": "分辨率", "type": "select", "default": "720p",
         "options": [["480p", "480p"], ["720p", "720p"], ["1080p", "1080p"]]},
    ]},
    {"id": "price", "title": "估价", "fields": [
        {"key": "PRICE_EDIT_SEEDREAM", "label": "即梦出图（元/次）", "type": "number", "default": "0.25"},
        {"key": "PRICE_VIDEO_SEEDANCE", "label": "转身视频（元/段）", "type": "number", "default": "1.5"},
        {"key": "PRICE_TRYON_BAILIAN", "label": "百炼试衣（元/次）", "type": "number", "default": "0.5"},
        {"key": "PRICE_REFINE_BAILIAN", "label": "百炼精修（元/次）", "type": "number", "default": "0.5"},
        {"key": "PRICE_EDIT_BAILIAN", "label": "百炼图像编辑（元/次）", "type": "number", "default": "0.3"},
        {"key": "PRICE_TRYON_KLING", "label": "可灵试穿（元/次）", "type": "number", "default": "0.5"},
    ]},
    {"id": "local", "title": "本地文件", "fields": [
        {"key": "REMBG_MODEL", "label": "本地抠图模型", "type": "select", "default": "u2net",
         "options": [["u2net", "u2net（默认）"], ["u2netp", "u2netp（更小更快）"], ["isnet-general-use", "isnet-general-use"],
                     ["silueta", "silueta（很小）"]],
         "help": "单品抠图用，第一次用某个模型会下载约 170MB"},
    ]},
]

FIELDS = {f["key"]: f for g in GROUPS for f in g["fields"]}
SECRET = {k for k, f in FIELDS.items() if f.get("type") == "secret"}

# 启动时（还没被设置面板覆盖前）各项的 .env / 系统环境变量值
ENV_BASE = {k: os.environ.get(k) for k in FIELDS}


def effective(key, stored):
    """某一项现在生效的值和来源：db（面板里保存的）/ env（.env）/ default。"""
    if key in stored:
        return stored[key], "db"
    if ENV_BASE.get(key):
        return ENV_BASE[key], "env"
    return FIELDS[key].get("default", ""), "default"


def apply(stored):
    """把面板里保存的值写进进程环境变量，providers.py 里的 env() 马上就读得到。清掉的项恢复成 .env 的值。"""
    for k in FIELDS:
        if k in stored:
            os.environ[k] = stored[k]
        elif ENV_BASE.get(k) is not None:
            os.environ[k] = ENV_BASE[k]
        else:
            os.environ.pop(k, None)


def mask(v):
    return "" if not v else ("••••" + v[-4:] if len(v) > 8 else "••••")


def public(stored):
    """发给浏览器的设置：密钥只给“有没有填”和末四位，不给明文。"""
    groups = []
    for g in GROUPS:
        fields = []
        for f in g["fields"]:
            v, src = effective(f["key"], stored)
            d = {k: f[k] for k in ("key", "label", "type", "options", "help", "default") if k in f}
            d.setdefault("type", "text")
            d["source"] = src if v else "none"
            if f["key"] in SECRET:
                d["value"], d["set"] = "", bool(v)
                d["hint"] = mask(v)
            else:
                d["value"] = v
            fields.append(d)
        groups.append({"id": g["id"], "title": g["title"], "fields": fields})
    return groups


def clean(key, value):
    """校验一项设置的值，返回存进去的字符串。"""
    f = FIELDS[key]
    value = str(value).strip()
    t = f.get("type", "text")
    if not value:
        return ""
    if t == "number" and value:
        try:
            float(value)
        except ValueError:
            raise ValueError(f"“{f['label']}”要填数字。")
    if t == "select" and value not in [o[0] for o in f["options"]]:
        raise ValueError(f"“{f['label']}”的选项不对。")
    if t == "toggle" and value not in ("0", "1"):
        raise ValueError(f"“{f['label']}”只能开或关。")
    if key.endswith("_BASE_URL") and value and not value.startswith(("http://", "https://")):
        raise ValueError(f"“{f['label']}”要以 http:// 或 https:// 开头。")
    if "\n" in value or "\r" in value:
        raise ValueError(f"“{f['label']}”不能换行。")
    return value
