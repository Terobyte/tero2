import pytest
from textual.app import App
from textual.widgets import Label


@pytest.mark.asyncio
async def test_role_swap_zai_appears_in_providers():
    from tero2.tui.screens.role_swap import RoleSwapScreen
    app = App()
    async with app.run_test(headless=True) as pilot:
        screen = RoleSwapScreen(roles=["builder"])
        await app.push_screen(screen, lambda x: None)
        await pilot.pause(0.1)
        await pilot.press("enter")  # select first role -> step 2
        await pilot.pause(0.1)
        items = screen.query("ListView ListItem")
        all_text = " ".join(
            str(item.query_one(Label).content)
            for item in items
        )
        assert "zai" in all_text.lower()


@pytest.mark.asyncio
async def test_role_swap_gemma_has_disabled_class():
    from tero2.tui.screens.role_swap import RoleSwapScreen
    app = App()
    async with app.run_test(headless=True) as pilot:
        screen = RoleSwapScreen(roles=["builder"])
        await app.push_screen(screen, lambda x: None)
        await pilot.pause(0.1)
        await pilot.press("enter")
        await pilot.pause(0.1)
        disabled = screen.query(".provider-disabled")
        assert len(disabled) >= 1


@pytest.mark.asyncio
async def test_role_swap_switch_message_has_model():
    from unittest.mock import AsyncMock, patch
    from tero2.providers.catalog import ModelEntry
    from tero2.tui.screens.role_swap import RoleSwapScreen, SwitchProviderMessage

    fake_models = [ModelEntry(id="sonnet", label="Sonnet")]

    messages = []

    class _TestApp(App):
        def on_switch_provider_message(self, msg: SwitchProviderMessage) -> None:
            messages.append(msg)

    with patch("tero2.tui.screens.role_swap.get_models", new=AsyncMock(return_value=fake_models)):
        app = _TestApp()
        async with app.run_test(headless=True) as pilot:
            screen = RoleSwapScreen(roles=["builder"])
            await app.push_screen(screen, lambda x: None)
            await pilot.pause(0.1)
            await pilot.press("enter")   # select role
            await pilot.pause(0.2)
            await pilot.press("enter")   # select first provider (claude)
            await pilot.pause(0.4)       # worker + push_screen need time
            await pilot.press("enter")   # select first model (sonnet)
            await pilot.pause(0.4)

    assert len(messages) >= 1
    assert hasattr(messages[0], "model")
    assert messages[0].model == "sonnet"
