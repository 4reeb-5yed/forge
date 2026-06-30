"""Property-based tests for secret non-leakage using Hypothesis.

**Validates: Requirements 22.1, 22.2, 22.3**

Property 9: Secret non-leakage — for all persisted artifacts (state snapshots,
    audit records, event payloads), no raw VCS token or provider key appears
    after calling redact_or_raise().
"""

from __future__ import annotations

from hypothesis import given, settings, strategies as st

from app.runtime.secrets import REDACTED_PLACEHOLDER, RedactionError, SecretHolder


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Generate realistic secret values: tokens/keys are typically 8+ characters
# of alphanumeric content. We use a minimum of 4 to avoid trivial substrings
# that would appear in normal text (like "0" or "*").
# We also filter out values that are substrings of the REDACTED_PLACEHOLDER
# since those would make post-redaction verification impossible (the secret
# would appear inside the placeholder itself — a correct abort scenario per
# requirement 22.7, but not useful for testing the core redaction property).
secret_values = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N")),
    min_size=4,
    max_size=50,
).filter(lambda s: s not in REDACTED_PLACEHOLDER and REDACTED_PLACEHOLDER.find(s) == -1)

# Generate session IDs: non-empty alphanumeric identifiers prefixed to avoid
# overlap with secrets.
identifiers = st.builds(
    lambda s: f"sess_{s}",
    st.text(
        alphabet=st.characters(whitelist_categories=("L", "N")),
        min_size=1,
        max_size=15,
    ),
)

# Generate arbitrary leaf values for data structures
leaf_values = st.one_of(
    st.text(min_size=0, max_size=100),
    st.integers(min_value=-1000, max_value=1000),
    st.booleans(),
    st.none(),
)


def data_structures(secrets: list[str]) -> st.SearchStrategy:
    """Generate data structures that may embed secret values.

    Produces strings, dicts, lists, and nested combinations that include
    the provided secrets mixed with arbitrary non-secret content.
    """
    # Leaf: either a random value or one of the secrets injected directly
    secret_or_leaf = st.one_of(
        leaf_values,
        st.sampled_from(secrets) if secrets else st.text(min_size=0, max_size=20),
    )

    # Strings that may contain secrets embedded in surrounding text
    strings_with_secrets = st.one_of(
        # Raw secret value as the entire string
        st.sampled_from(secrets) if secrets else st.text(min_size=0, max_size=20),
        # Secret embedded in a larger string
        st.builds(
            lambda prefix, s, suffix: f"{prefix}{s}{suffix}",
            prefix=st.text(min_size=0, max_size=20),
            s=st.sampled_from(secrets) if secrets else st.just(""),
            suffix=st.text(min_size=0, max_size=20),
        ),
        # Plain text without secrets
        st.text(min_size=0, max_size=50),
    )

    # Recursive data structures
    return st.recursive(
        base=st.one_of(secret_or_leaf, strings_with_secrets),
        extend=lambda children: st.one_of(
            # Lists of children
            st.lists(children, min_size=0, max_size=5),
            # Dicts with string keys and children as values
            st.dictionaries(
                keys=st.one_of(
                    st.text(min_size=1, max_size=15),
                    st.sampled_from(secrets) if secrets else st.text(min_size=1, max_size=15),
                ),
                values=children,
                min_size=0,
                max_size=5,
            ),
        ),
        max_leaves=20,
    )


# ---------------------------------------------------------------------------
# Helper: check if any secret appears in data
# ---------------------------------------------------------------------------


def contains_any_secret(data, secrets: set[str]) -> bool:
    """Recursively check if any raw secret value appears in a data structure."""
    if isinstance(data, str):
        return any(secret in data for secret in secrets)
    elif isinstance(data, dict):
        for k, v in data.items():
            if contains_any_secret(k, secrets):
                return True
            if contains_any_secret(v, secrets):
                return True
        return False
    elif isinstance(data, (list, tuple)):
        return any(contains_any_secret(item, secrets) for item in data)
    return False


# ---------------------------------------------------------------------------
# Property 9: Secret non-leakage
# ---------------------------------------------------------------------------


class TestSecretNonLeakageProperty:
    """Property 9: Secret non-leakage — for all persisted artifacts (state
    snapshots, audit records, event payloads), no raw VCS token or provider
    key appears after redact_or_raise().

    **Validates: Requirements 22.1, 22.2, 22.3**
    """

    @given(
        session_id=identifiers,
        secrets=st.lists(secret_values, min_size=1, max_size=5),
        data=st.data(),
    )
    @settings(max_examples=200, deadline=5000)
    def test_no_raw_secret_in_redacted_output(
        self,
        session_id: str,
        secrets: list[str],
        data: st.DataObject,
    ) -> None:
        """For arbitrary secrets stored in the SecretHolder and arbitrary data
        structures that may contain those secrets, after calling redact_or_raise(),
        either no raw secret appears in the output OR the operation is aborted
        with RedactionError (both satisfy non-leakage).
        """
        holder = SecretHolder()

        # Store all generated secrets under different key names
        key_names = [f"key_{i}" for i in range(len(secrets))]
        for key_name, secret_val in zip(key_names, secrets):
            holder.store_secret(session_id, key_name, secret_val)

        # Generate a data structure that may contain the secrets
        input_data = data.draw(
            data_structures(secrets),
            label="input_data",
        )

        # Redact the data — either succeeds cleanly or aborts (both are safe)
        try:
            redacted = holder.redact_or_raise(input_data, session_id=session_id)
        except RedactionError:
            # Operation aborted — no secret leaked (requirement 22.7)
            return

        # Verify: no raw secret appears in the redacted output
        secret_set = set(secrets)
        assert not contains_any_secret(redacted, secret_set), (
            f"Raw secret leaked through redaction!\n"
            f"Secrets: {secrets}\n"
            f"Input: {input_data}\n"
            f"Redacted output: {redacted}"
        )

    @given(
        session_id=identifiers,
        secrets=st.lists(secret_values, min_size=1, max_size=3),
        data=st.data(),
    )
    @settings(max_examples=200, deadline=5000)
    def test_no_raw_secret_in_redacted_nested_dicts(
        self,
        session_id: str,
        secrets: list[str],
        data: st.DataObject,
    ) -> None:
        """Specifically test nested dict structures (simulating state snapshots
        and audit records) — no raw secret leaks after redaction.
        """
        holder = SecretHolder()

        for i, secret_val in enumerate(secrets):
            holder.store_secret(session_id, f"provider_key_{i}", secret_val)

        # Build a nested dict simulating a state snapshot or audit record
        inner_values = st.one_of(
            st.text(min_size=0, max_size=30),
            st.sampled_from(secrets),
            st.builds(
                lambda prefix, s: f"{prefix}{s}",
                prefix=st.text(min_size=0, max_size=10),
                s=st.sampled_from(secrets),
            ),
            st.integers(),
            st.none(),
        )

        snapshot = data.draw(
            st.fixed_dictionaries({
                "session_id": st.just(session_id),
                "status": st.sampled_from(["running", "paused", "done"]),
                "auth": inner_values,
                "config": st.dictionaries(
                    keys=st.text(min_size=1, max_size=10),
                    values=inner_values,
                    min_size=0,
                    max_size=4,
                ),
                "tasks": st.lists(
                    st.fixed_dictionaries({
                        "name": st.text(min_size=1, max_size=15),
                        "output": inner_values,
                    }),
                    min_size=0,
                    max_size=3,
                ),
            }),
            label="snapshot",
        )

        try:
            redacted = holder.redact_or_raise(snapshot, session_id=session_id)
        except RedactionError:
            # Operation aborted — no secret leaked (requirement 22.7)
            return

        secret_set = set(secrets)
        assert not contains_any_secret(redacted, secret_set), (
            f"Raw secret leaked in nested dict structure!\n"
            f"Secrets: {secrets}\n"
            f"Input snapshot: {snapshot}\n"
            f"Redacted: {redacted}"
        )

    @given(
        session_id=identifiers,
        secrets=st.lists(secret_values, min_size=1, max_size=3),
        data=st.data(),
    )
    @settings(max_examples=200, deadline=5000)
    def test_no_raw_secret_in_redacted_event_payloads(
        self,
        session_id: str,
        secrets: list[str],
        data: st.DataObject,
    ) -> None:
        """Specifically test event payload structures — no raw secret leaks
        through event payload redaction.
        """
        holder = SecretHolder()

        for i, secret_val in enumerate(secrets):
            holder.store_secret(session_id, f"key_{i}", secret_val)

        # Generate event-like payload with potential secret leakage
        payload_values = st.one_of(
            st.text(min_size=0, max_size=40),
            st.sampled_from(secrets),
            st.builds(
                lambda t, s, sfx: f"{t}{s}{sfx}",
                t=st.sampled_from(["token=", "Bearer ", "key:", "auth "]),
                s=st.sampled_from(secrets),
                sfx=st.text(min_size=0, max_size=10),
            ),
        )

        payload = data.draw(
            st.fixed_dictionaries({
                "type": st.sampled_from([
                    "model.selected", "commit.done", "task.start",
                    "verify.stage", "build.done",
                ]),
                "detail": payload_values,
                "metadata": st.dictionaries(
                    keys=st.text(min_size=1, max_size=10),
                    values=payload_values,
                    min_size=0,
                    max_size=3,
                ),
            }),
            label="event_payload",
        )

        try:
            redacted = holder.redact_or_raise(payload, session_id=session_id)
        except RedactionError:
            # Operation aborted — no secret leaked (requirement 22.7)
            return

        secret_set = set(secrets)
        assert not contains_any_secret(redacted, secret_set), (
            f"Raw secret leaked in event payload!\n"
            f"Secrets: {secrets}\n"
            f"Input payload: {payload}\n"
            f"Redacted: {redacted}"
        )

    @given(
        session_id=identifiers,
        secrets=st.lists(secret_values, min_size=2, max_size=5),
        data=st.data(),
    )
    @settings(max_examples=100, deadline=5000)
    def test_multiple_secrets_all_redacted(
        self,
        session_id: str,
        secrets: list[str],
        data: st.DataObject,
    ) -> None:
        """When multiple secrets exist, ALL of them must be redacted from
        any data structure, not just one.
        """
        holder = SecretHolder()

        for i, secret_val in enumerate(secrets):
            holder.store_secret(session_id, f"key_{i}", secret_val)

        # Create a string that contains ALL secrets concatenated
        separator = data.draw(st.text(min_size=0, max_size=5), label="separator")
        combined = separator.join(secrets)

        try:
            redacted = holder.redact_or_raise(combined, session_id=session_id)
        except RedactionError:
            # Operation aborted — no secret leaked (requirement 22.7)
            return

        # No individual secret should appear in the output
        for secret in secrets:
            assert secret not in redacted, (
                f"Secret '{secret}' leaked through multi-secret redaction!\n"
                f"All secrets: {secrets}\n"
                f"Combined input: {combined}\n"
                f"Redacted: {redacted}"
            )

    @given(
        data=st.data(),
    )
    @settings(max_examples=100, deadline=5000)
    def test_token_reference_does_not_contain_raw_secret(
        self,
        data: st.DataObject,
    ) -> None:
        """Token references stored in state snapshots instead of raw values
        must never contain the actual secret value (Requirement 22.3).
        """
        session_id = data.draw(identifiers, label="session_id")
        # Ensure secret is not a substring of the session_id or key name
        secret = data.draw(
            secret_values.filter(
                lambda s: s not in session_id and s not in "vcs_token"
            ),
            label="secret",
        )

        holder = SecretHolder()
        holder.store_secret(session_id, "vcs_token", secret)

        ref = holder.get_token_reference(session_id, "vcs_token")

        # The string representation of the reference must not contain the secret
        ref_str = str(ref)
        assert secret not in ref_str, (
            f"Token reference contains raw secret!\n"
            f"Secret: {secret}\n"
            f"TokenReference str: {ref_str}"
        )

        # The placeholder must not be the secret itself
        assert ref.placeholder != secret, (
            f"Token reference placeholder IS the secret: {secret}"
        )
