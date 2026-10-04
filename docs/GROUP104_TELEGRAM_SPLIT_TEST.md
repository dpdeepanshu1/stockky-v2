# group104 (2026-10-04) - test fix: Telegram long-message split test expected the wrong part count

Cumulative on group103. Test-only change; no production code changed, so there is nothing to rebuild.

## What was failing
`notification-scheduler-service/tests/test_telegram_long_message_split.py::test_send_telegram_long_message_goes_out_in_numbered_parts` asserted `2 <= parts <= 4`. Its message is 22,279 characters and `_TELEGRAM_PART_CHARS` is 3,800, so the correct count is 6. The splitter was right: six parts of 3,679-3,749 characters, each under Telegram's 4,096 limit, and joining them reproduces the message exactly. The test had failed since it was written (it also fails on the group99 upload).

## Fix
The test now derives its expectation from the splitter's own limit: parts == `_split_for_telegram(msg, _TELEGRAM_PART_CHARS)` and parts == ceil(len / 3800) (so no wasted extra parts), plus at least 2. Every other assertion in that test (each body under 4,096, numbered titles, first and last symbol present) is unchanged.

## Run here
`notification-scheduler-service`: 178 passed (was 177 passed, 1 failed).

## Not changed
Whether 3,800 is the best part size (Telegram's limit is 4,096 and HTML tags are added around each part) was not touched.
