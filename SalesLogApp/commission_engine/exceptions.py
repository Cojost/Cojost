class CommissionEngineError(Exception):
    pass


class PayPlanResolutionError(CommissionEngineError):
    pass


class RuleConfigurationError(CommissionEngineError):
    pass


class UnsupportedRuleTypeError(CommissionEngineError):
    pass


class ConditionValidationError(CommissionEngineError):
    pass


class CalculationError(CommissionEngineError):
    pass
