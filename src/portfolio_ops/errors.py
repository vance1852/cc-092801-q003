"""实验样本事件快处服务向 API 和 CLI 暴露的稳定错误。"""


class CollectionDispatchError(RuntimeError):
    code = "traffic_error"
    status = 400


class NotFound(CollectionDispatchError):
    code = "not_found"
    status = 404


class Conflict(CollectionDispatchError):
    code = "conflict"
    status = 409


class Forbidden(CollectionDispatchError):
    code = "forbidden"
    status = 403


class InvalidState(CollectionDispatchError):
    code = "invalid_state"
    status = 409


class ValidationFailed(CollectionDispatchError):
    code = "validation_failed"
    status = 422


class BusinessRuleViolation(CollectionDispatchError):
    """请求格式正确，但业务证据（如情景绑定的指标系列/版本/日期）不满足运行条件。"""

    code = "business_rule_violated"
    status = 422
