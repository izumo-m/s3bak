"""The --delete confirmation decider (confirm.py) via the console prompt seam."""

from __future__ import annotations

import pytest

from s3bak import confirm
from s3bak.confirm import AnswerMode, DeleteConfirmer, DeletionAbortedError


@pytest.fixture(autouse=True)
def _fresh_abort_state():
    confirm.reset_confirmations()
    yield
    confirm.reset_confirmations()


def test_resolve_answer_mode_matrix(monkeypatch):
    assert confirm.resolve_answer_mode(yes=True) is AnswerMode.ALL_YES
    monkeypatch.setattr(confirm, "prompt_is_interactive", lambda: True)
    assert confirm.resolve_answer_mode(yes=False) is AnswerMode.ASK
    monkeypatch.setattr(confirm, "prompt_is_interactive", lambda: False)
    assert confirm.resolve_answer_mode(yes=False) is AnswerMode.ALL_NO


def test_non_interactive_is_the_default_under_pytest():
    # pytest's stdin is not a TTY, so the auto-n path needs no monkeypatching.
    assert confirm.resolve_answer_mode(yes=False) is AnswerMode.ALL_NO


def test_all_yes_deletes_without_prompting(answers):
    confirmer = DeleteConfirmer(AnswerMode.ALL_YES, "data")
    assert confirmer.confirm("x") is True
    assert answers.prompts == []


def test_all_no_keeps_without_prompting(answers):
    confirmer = DeleteConfirmer(AnswerMode.ALL_NO, "data")
    assert confirmer.confirm("x") is False
    assert confirmer.confirm("y") is False
    assert answers.prompts == []


def test_interactive_y_n_sequencing(answers):
    answers.feed("y", "n")
    confirmer = DeleteConfirmer(AnswerMode.ASK, "data")
    assert confirmer.confirm("a.txt") is True
    assert confirmer.confirm("b.txt") is False
    assert len(answers.prompts) == 2
    assert "data" in answers.prompts[0]
    assert "a.txt" in answers.prompts[0]


def test_answer_a_deletes_everything_after(answers):
    answers.feed("a")
    confirmer = DeleteConfirmer(AnswerMode.ASK, "data")
    assert confirmer.confirm("a.txt") is True
    assert confirmer.confirm("b.txt") is True
    assert len(answers.prompts) == 1


def test_answer_d_keeps_everything_after(answers):
    answers.feed("d")
    confirmer = DeleteConfirmer(AnswerMode.ASK, "data")
    assert confirmer.confirm("a.txt") is False
    assert confirmer.confirm("b.txt") is False
    assert len(answers.prompts) == 1


def test_invalid_and_empty_answers_print_the_legend_and_reprompt(answers, capfd):
    # A bare Enter must re-ask, never abort: only EOF (None) counts as q.
    answers.feed("", "maybe", "y")
    confirmer = DeleteConfirmer(AnswerMode.ASK, "data")
    assert confirmer.confirm("a.txt") is True
    assert len(answers.prompts) == 3
    assert capfd.readouterr().err.count("q, quit - abort the whole command") == 2


def test_question_mark_prints_the_legend(answers, capfd):
    answers.feed("?", "n")
    confirmer = DeleteConfirmer(AnswerMode.ASK, "data")
    assert confirmer.confirm("a.txt") is False
    assert len(answers.prompts) == 2
    assert "[y/n/a/d/q/?]" in answers.prompts[0]
    assert "y, yes  - delete this one" in capfd.readouterr().err


def test_full_word_answers(answers):
    answers.feed("yes", "no", "all")
    confirmer = DeleteConfirmer(AnswerMode.ASK, "data")
    assert confirmer.confirm("a.txt") is True
    assert confirmer.confirm("b.txt") is False
    assert confirmer.confirm("c.txt") is True
    assert confirmer.confirm("d.txt") is True  # all is sticky
    assert len(answers.prompts) == 3


def test_quit_word_aborts(answers):
    answers.feed("quit")
    confirmer = DeleteConfirmer(AnswerMode.ASK, "data")
    with pytest.raises(DeletionAbortedError):
        confirmer.confirm("a.txt")


def test_delete_word_is_not_an_answer(answers, capfd):
    # d means keep, so the word "delete" must never be accepted as an answer;
    # like any unrecognized input it prints the legend and re-asks.
    answers.feed("delete", "n")
    confirmer = DeleteConfirmer(AnswerMode.ASK, "data")
    assert confirmer.confirm("a.txt") is False
    assert len(answers.prompts) == 2
    assert "d       - keep this one" in capfd.readouterr().err


def test_answer_summary_prints_once_per_run(answers, capfd):
    answers.feed("y", "y")
    confirmer = DeleteConfirmer(AnswerMode.ASK, "data")
    assert confirmer.confirm("a.txt") is True
    assert confirmer.confirm("b.txt") is True
    assert capfd.readouterr().err.count("--delete answers:") == 1
    confirm.reset_confirmations()  # a new run reprints the summary
    answers.feed("y")
    fresh = DeleteConfirmer(AnswerMode.ASK, "data")
    assert fresh.confirm("a.txt") is True
    assert "--delete answers:" in capfd.readouterr().err


def test_q_aborts_this_and_every_later_confirmation(answers):
    answers.feed("q")
    confirmer = DeleteConfirmer(AnswerMode.ASK, "data")
    other = DeleteConfirmer(AnswerMode.ASK, "other-entry")
    with pytest.raises(DeletionAbortedError):
        confirmer.confirm("a.txt")
    # A sibling --all entry stops without prompting.
    with pytest.raises(DeletionAbortedError):
        other.confirm("b.txt")
    assert len(answers.prompts) == 1


def test_eof_mid_prompt_aborts(answers):
    # No queued answer -> the fake seam returns "" like a closed stdin.
    confirmer = DeleteConfirmer(AnswerMode.ASK, "data")
    with pytest.raises(DeletionAbortedError):
        confirmer.confirm("a.txt")


def test_reset_confirmations_clears_the_abort_flag(answers):
    answers.feed("q")
    confirmer = DeleteConfirmer(AnswerMode.ASK, "data")
    with pytest.raises(DeletionAbortedError):
        confirmer.confirm("a.txt")
    confirm.reset_confirmations()
    answers.feed("y")
    fresh = DeleteConfirmer(AnswerMode.ASK, "data")
    assert fresh.confirm("a.txt") is True


def test_confirm_subtree_delete_modes(answers, capfd):
    assert confirm.confirm_subtree_delete(AnswerMode.ALL_YES, "data", "sub") is True
    assert confirm.confirm_subtree_delete(AnswerMode.ALL_NO, "data", "sub") is False
    assert answers.prompts == []
    answers.feed("y")
    assert confirm.confirm_subtree_delete(AnswerMode.ASK, "data", "sub") is True
    answers.feed("no")
    assert confirm.confirm_subtree_delete(AnswerMode.ASK, "data", "sub") is False
    answers.feed("bogus", "")  # unrecognized answers explain and re-ask; EOF answers n
    assert confirm.confirm_subtree_delete(AnswerMode.ASK, "data", "sub") is False
    assert "data" in answers.prompts[0]
    assert capfd.readouterr().err.count("keep it") == 2


def test_confirm_subtree_delete_rechecks_abort_after_acquiring_lock(monkeypatch):
    # Another entry answers q (sets _abort) while this subtree confirmation is
    # blocked waiting for the prompt lock. After acquiring the lock it must
    # re-check _abort and abort, not show the prompt and delete on a y.
    monkeypatch.setattr(confirm, "read_prompt_answer", lambda _p: "y")

    class AbortOnEnter:
        def __enter__(self):
            confirm._abort.set()  # simulate the concurrent q during the lock wait
            return self

        def __exit__(self, *_a):
            return False

    monkeypatch.setattr(confirm, "_prompt_lock", AbortOnEnter())
    with pytest.raises(DeletionAbortedError):
        confirm.confirm_subtree_delete(AnswerMode.ASK, "entry", "s3://bucket/entry/sub")
