"""共享异常类型。"""


class ZhiyunError(Exception):
    """工具内所有可预期错误的基类。"""


class LoginError(ZhiyunError):
    """登录失败（账号密码、验证码、OAuth 等）。"""


class AuthExpiredError(ZhiyunError):
    """智云凭证缺失、失效或权限不足。"""


class ApiError(ZhiyunError):
    """接口返回异常。"""


class ExportError(ZhiyunError):
    """导出过程失败。"""


class EnvFileError(ZhiyunError):
    """读取 .env 文件失败。"""
