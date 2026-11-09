# J04 Switch between browser and terminal answers

```
ID: J04
Purpose: Whichever answer comes first counts, once; switching sides never loses or repeats a question
Preconditions: a fresh install under way with the setup window open beside the terminal
Actions: answer the first optional question in the window; answer the next in the terminal;
  answer the backup-times page in the window (type the weekday as "sat" in the terminal once);
  answer the plain question after the page with the window's Send; finish in the terminal
Expected: see below
Evidence: terminal log; the window's event log (setup_progress events); the saved answers in
  the settings; the timers written
Cleanup: restore the post-install snapshot
```

## Expected results

- Each question is answered once, by whichever side is first
  (`test_the_window_or_the_terminal_answers_whichever_is_first`, tests/test_setup_gui.py:192;
  `test_the_terminal_still_answers`, tests/test_setup_gui.py:739). The window shows the
  terminal's answer and moves on; the terminal shows the window's.
- A question asked again keeps its list of choices
  (`test_a_question_asked_again_keeps_its_list`, tests/test_setup_gui.py:155).
- A program that asks in the terminal (Tailscale's sign-in, onboarding) is handed off
  there and the window says so (`test_a_program_asking_in_the_terminal_is_handed_off_there`,
  tests/test_setup_gui.py:476; `test_onboarding_asks_in_the_terminal_with_the_window_told`,
  tests/test_setup_gui.py:497).
- The backup-times page answers the three questions as one
  (`test_the_setup_window_answers_the_three_as_one_page`, tests/test_backup_times.py:290;
  `test_the_backup_times_are_one_page_of_three_rows`, tests/test_setup_flow.py:211); a field
  the window sent wrong is asked again in the terminal
  (`test_a_field_the_window_sent_wrong_is_asked_in_the_terminal`, tests/test_backup_times.py:324).
- After the page, Send works for the plain question that follows: the page's rows are
  removed, not hidden (`test_the_pages_rows_go_when_a_plain_question_follows`,
  tests/test_setup_flow.py:257; B-0481-10).
- `sat` typed in the terminal reaches the page as `Sat` and the day selector shows it
  (`test_a_weekday_typed_in_lowercase_reaches_the_page_as_the_day_itself`,
  tests/test_backup_times.py:583; B-0481-05).
- The window's event log holds the questions and never the answers
  (`test_questions_are_seen_while_they_wait_and_never_their_answers`, tests/test_setup_gui.py:86).
- The timers written at the end hold the times answered, whichever side answered them.

## Manual parts

Not driven by the rig yet: the rig's rule driver answers in the terminal only. Until a
window driver exists, this journey is run by hand on the VM with the window open (the
`gui-run.sh` way: a delay before each terminal answer so the question is visible).

0.48.1 gate: not tested (unit tests only).
