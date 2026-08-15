import asyncio
import logging

import httpx
import pytest

from valueroute.providers.openai import OpenAIProviderAdapter, ProviderCallError


def test_provider_http_error_does_not_expose_secret_or_private_body():
    secret = "sk-test-secret-do-not-leak"
    private_body = '{"customer_ssn":"000-00-0000","instruction":"private body"}'

    async def handler(request: httpx.Request):
        return httpx.Response(
            401,
            json={"error": {"message": f"{private_body} authorization={secret}"}},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def run():
        with pytest.raises(ProviderCallError) as raised:
            await OpenAIProviderAdapter(model_id="model", api_key=secret, client=client).complete(
                task_id="task", input_text=private_body
            )
        await client.aclose()
        return str(raised.value)

    message = asyncio.run(run())
    assert message == "provider returned HTTP 401"
    assert secret not in message
    assert private_body not in message


def test_provider_failure_does_not_write_secret_or_private_body_to_logs(caplog, tmp_path):
    secret = "provider-secret-do-not-leak"
    private_body = "private-request-body-do-not-leak"

    class Provider:
        async def complete(self, **kwargs):
            raise RuntimeError(f"upstream failed: {secret}; body={private_body}")

    from valueroute.domain.models import WorkerAttempt, WorkerAttemptStatus
    from valueroute.execution.runner import WorkerRunner
    from valueroute.storage.journal import LocalJournal
    from valueroute.storage.store import Store

    journal = LocalJournal(tmp_path)
    store = Store(journal)
    attempt = WorkerAttempt(id="attempt_sensitive", worker_session_id="session", child_task_id="child")
    store.attempts[attempt.id] = attempt
    store.attempt_session[attempt.id] = "controller"

    with caplog.at_level(logging.DEBUG):
        result = asyncio.run(
            WorkerRunner(store, Provider()).run(
                attempt.id,
                task_id="task",
                input_text=private_body,
            )
        )

    journal.close()
    assert result.status is WorkerAttemptStatus.failed
    assert secret not in caplog.text
    assert private_body not in caplog.text

