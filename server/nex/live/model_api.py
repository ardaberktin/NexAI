"""Authenticated settings for the shared Excalibur language model."""

from fastapi import HTTPException, Request
from pydantic import BaseModel

from .access import workspace_for


class Selection(BaseModel):
    model: str


def install_model_api(app, authenticate):
    def runtime(request):
        if not authenticate(request):
            raise HTTPException(401)
        if app.state.models is None:
            raise HTTPException(503, "Install the updated Nex live launcher on Excalibur.")
        return app.state.models

    @app.get("/v2/live-model")
    @app.get("/v1/live-model")
    async def model_status(request: Request):
        return runtime(request).status()

    @app.put("/v2/live-model")
    @app.put("/v1/live-model")
    async def switch_model(request: Request, selection: Selection):
        models = runtime(request)
        client = request.scope.get("nex.client")
        if client and not client.account.owner:
            raise HTTPException(403)
        state = workspace_for(request)
        if state.active.locked():
            raise HTTPException(409, "End the active conversation or wait for the current switch to finish.")
        if models.status()["active"] == selection.model and not models.lock.locked():
            return models.status()
        async with state.active, state.attention.busy, state.dialogue.background_slot:
            try:
                return await models.select(selection.model)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            except (OSError, RuntimeError) as exc:
                raise HTTPException(503, "Excalibur could not activate the selected model.") from exc
