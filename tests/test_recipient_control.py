import pytest

from dots_phone.recipient_control import recipient_requests_stop


@pytest.mark.parametrize(
    "text",
    [
        "If that is all you need, please hang up to hear that again say repeat or say main menu for more options.",
        "If you're done, hang up. Otherwise say main menu.",
        "Hang up if you have everything you need.",
        "You may hang up now or say main menu for more options.",
        "You can say repeat that or to do something else. Say main menu or if that's all you need. Feel free to hang up",
        "Feel free to hang up whenever you're done.",
        "Press two to end the call.",
        "To hang up, press two.",
        "To end the call, press the pound key.",
        "Please don't hang up. I'll check the account.",
        "Don't stop calling me until this is resolved.",
        "Stop the story and check the balance.",
        "You're welcome to hang up when finished.",
        "You are free to end the call whenever you like.",
        "You can either hang up or say main menu.",
        "To continue, say main menu. Otherwise you can hang up.",
        "If that is all you need. Please hang up.",
        "When you're done. Hang up.",
        "Hang up. If you're finished.",
        "Hang up. Or say main menu for other options.",
        "If you need nothing else, please. Hang up.",
        "You may now. Hang up.",
        "Say hang up to end the call.",
        "For the hang up option, press three.",
        "Press one. To end the call.",
        'The message says "please hang up" but you should stay on the line.',
        'When I say "stop calling", that is the phrase to use.',
        "I didn't ask you to hang up.",
        "Do not hang up.",
        "Never stop calling me until the problem is fixed.",
        "Stop calling is the name of that menu option.",
    ],
)
def test_optional_menu_instructions_do_not_request_call_stop(text):
    assert not recipient_requests_stop(text)


@pytest.mark.parametrize(
    "text",
    [
        "Thank you. End the call now.",
        "Please hang up.",
        "Could you please hang up?",
        "Stop calling me.",
        "Do not call this number again.",
        "If you are a robot, stop calling me.",
        "If that's all you need, hang up. Actually, please stop calling me.",
        "Please, hang up.",
        "Please stop calling, thank you.",
        "Can you end this call for me?",
        "Would you please hang up now?",
        "I want you to hang up.",
        "I need you to end this call immediately.",
        "I'd like you to stop calling me.",
        "You need to hang up now.",
        "Thanks, please hang up now.",
        "Don't call us anymore.",
        "Stop this call.",
        "Could you please stop calling this number?",
        "If that's all you need, hang up. Please end this call now.",
        "Now please hang up.",
        "Stop calling me, I am not interested.",
        "Do not call my phone again.",
        "Please hang up, I have to go.",
        "Please end this call because I don't want to continue.",
        "Please stop calling me, I'm not interested in talking to you.",
        "Don't call my office again, remove this number.",
        "Stop calling me and leave me alone.",
    ],
)
def test_explicit_recipient_stop_requests_are_honored(text):
    assert recipient_requests_stop(text)
