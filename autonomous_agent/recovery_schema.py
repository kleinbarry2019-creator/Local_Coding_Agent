class StateSchemaValidator:
    """Validate persisted agent state before recovery logic trusts it."""

    SUPPORTED_VERSION = 1

    ALLOWED_FIELDS = {
        "execution_blocked",
        "goal",
        "last_result",
        "last_trust_update",
        "plan",
        "recovery_attempts",
        "recovery_audit_chain",
        "recovery_blocked",
        "recovery_confidence",
        "recovery_event",
        "recovery_failures",
        "recovery_history",
        "recovery_reason",
        "recovery_successes",
        "recovery_trust",
        "result",
        "schema_version",
        "status",
        "tasks_completed",
        "updated",
        "version",
    }

    BOOLEAN_FIELDS = {
        "execution_blocked",
        "recovery_blocked",
        "recovery_event",
    }

    NON_NEGATIVE_INTEGER_FIELDS = {
        "recovery_attempts",
        "recovery_failures",
        "recovery_successes",
        "tasks_completed",
        "version",
    }

    UNIT_INTERVAL_FIELDS = {
        "recovery_confidence",
        "recovery_trust",
    }

    STRING_FIELDS = {
        "last_trust_update",
        "recovery_reason",
        "status",
        "updated",
    }

    LIST_FIELDS = {
        "plan",
        "recovery_audit_chain",
        "recovery_history",
    }

    def validate(self, state):
        errors = []

        if not isinstance(state, dict):
            return {
                "valid": False,
                "errors": ["state.expected_object"],
            }

        version = state.get("schema_version")

        if type(version) is not int:
            errors.append("schema_version.expected_integer")
        elif version != self.SUPPORTED_VERSION:
            errors.append("schema_version.unsupported")

        unknown_fields = sorted(set(state) - self.ALLOWED_FIELDS)
        errors.extend(
            f"field.unknown:{field}"
            for field in unknown_fields
        )

        for field in self.BOOLEAN_FIELDS:
            if field in state and type(state[field]) is not bool:
                errors.append(f"field.{field}.expected_boolean")

        for field in self.NON_NEGATIVE_INTEGER_FIELDS:
            if field not in state:
                continue

            value = state[field]
            if type(value) is not int or value < 0:
                errors.append(
                    f"field.{field}.expected_non_negative_integer"
                )

        for field in self.UNIT_INTERVAL_FIELDS:
            if field not in state:
                continue

            value = state[field]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0.0 <= value <= 1.0
            ):
                errors.append(f"field.{field}.expected_unit_interval")

        for field in self.STRING_FIELDS:
            if field in state and not isinstance(state[field], str):
                errors.append(f"field.{field}.expected_string")

        for field in self.LIST_FIELDS:
            if field in state and not isinstance(state[field], list):
                errors.append(f"field.{field}.expected_array")

        if "goal" in state and not isinstance(state["goal"], (dict, str)):
            errors.append("field.goal.expected_object_or_string")

        audit_chain = state.get("recovery_audit_chain")
        if isinstance(audit_chain, list):
            errors.extend(self._validate_audit_chain(audit_chain))

        recovery_history = state.get("recovery_history")
        if isinstance(recovery_history, list):
            errors.extend(self._validate_recovery_history(recovery_history))

        return {
            "valid": not errors,
            "errors": errors,
        }

    def _validate_audit_chain(self, chain):
        errors = []
        required = {
            "event": dict,
            "hash": str,
            "previous_hash": str,
            "timestamp": str,
        }

        for index, record in enumerate(chain):
            if not isinstance(record, dict):
                errors.append(
                    f"field.recovery_audit_chain[{index}].expected_object"
                )
                continue

            for field, expected_type in required.items():
                if not isinstance(record.get(field), expected_type):
                    errors.append(
                        "field.recovery_audit_chain"
                        f"[{index}].{field}.invalid"
                    )

        return errors

    def _validate_recovery_history(self, history):
        errors = []

        for index, record in enumerate(history):
            if not isinstance(record, dict):
                errors.append(
                    f"field.recovery_history[{index}].expected_object"
                )
                continue

            if not isinstance(record.get("timestamp"), str):
                errors.append(
                    f"field.recovery_history[{index}].timestamp.invalid"
                )

            if not isinstance(record.get("decision"), dict):
                errors.append(
                    f"field.recovery_history[{index}].decision.invalid"
                )

            if not isinstance(record.get("result"), dict):
                errors.append(
                    f"field.recovery_history[{index}].result.invalid"
                )

        return errors
