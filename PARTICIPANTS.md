# Taking part: do skills inferred from your AI history match how you want to work?

Thank you for helping! The tool reads your past conversations with AI assistants **on your own computer** and writes two sets of skills (short instruction files for an AI). You then compare them, and compare AI answers to a few of your own recent tasks, in a page on your computer. Only your choices are sent back; your conversations never leave your computer.

**Time needed:** about 40 minutes, once (about 20 minutes of waiting while the tool runs, about 20 minutes of comparing).

## What you need
- Python 3.10+ and the `claude` command-line tool (Claude Code), logged in. The tool uses your own login.
- Some history: ideally 100+ messages, with a few in the last two weeks. With less, you can still compare the skill sets (Step 2 of the page) but there will be few or no answers to compare.

## What the tool reads, and how much
- It reads **your own messages** (not the AI's replies, except the last few hundred characters of the reply before each message, so it can tell whether you were correcting something). Links, file paths and email addresses are masked first.
- It does **not** send everything. By default it sends at most **300 messages** to the AI. If you have more, it takes messages that look like steering first (words such as "shorter", "again", "not what I meant", "why", and short follow-ups), then fills the rest with a random sample of the others. The tool prints how many it used.
- Choose a period to keep it small and recent: `--since 2026-10-01 --until 2026-11-10`. Change the cap with `--max-messages 150`.
- Cost: roughly 100,000 to 200,000 tokens for 300 messages, plus the answers in Step 4. It uses your own Claude login, so it counts toward your own usage limit. If you are near your limit, use `--max-messages 150`.
- The last 14 days are always kept aside for the answer comparison, so they are not used to write skills.

## 1. Install
```bash
pipx install "git+https://github.com/ninacheung123/context-user.git"
# no pipx? use:  python3 -m pip install --user "git+https://github.com/ninacheung123/context-user.git"
mkdir -p ~/cu-study && cd ~/cu-study
```

## 2. Point it at your history
- **Claude Code and Cursor:** found automatically.
- **ChatGPT:** Settings → Data controls → Export data; unzip and note where `conversations.json` is.
- **claude.ai:** Settings → Privacy → Export data; same.

Add one `--chat-export …` per export file in every command below.

## 3. Run the three steps
```bash
cu study run --chat-export ~/Downloads/chatgpt/conversations.json   # about 10 minutes; add --since/--until/--max-messages to read less
cu study answers --n 5                                                # about 5 minutes
cu study page                                                         # prints a file to open in your browser
```
`run` makes two skill sets from your history and labels them X and Y at random. `answers` takes five of your recent tasks that can be answered from the text alone, and has the AI answer each in three ways (no skills, or with one of the two sets), shuffled as A, B, C. You are never told which is which.

## 4. Compare in the page
- **Step 1:** for each task, pick the best and worst answer. If you cannot tell, press *Cannot tell*. If the task is not a fair test (for example it needed a file), press *Skip*. A short reason helps but is optional.
- **Step 2** (appears after Step 1): mark each skill *Sounds like me / Not me / Unsure*, then say which set is closer to how you want an AI to work with you.
- Press **Save choices**; a file `choices.json` downloads.

## 5. Send your choices
```bash
cu study decode ~/Downloads/choices.json --out ratings.csv
```
Open `ratings.csv` first. It has a random participant code (made on your computer, not your name; you can edit it in `~/cu-study/.context/study_run/participant.txt` before decoding), your picks (which condition was best or worst, the set you preferred, your mark for each skill) and your short reasons. **It has no quotes from your conversations**, but your reasons are your own words, so read them. Delete any row you would rather not share. Send it to: [researcher email]

## Privacy and withdrawal
- Everything runs on your computer. Before anything reaches the AI, links, file paths and email addresses in your messages are masked.
- The skills and answers are written by Claude through your own `claude` login, as in normal use. The tool never uploads files.
- Your conversations, the skills, the answers and the page stay in `~/cu-study/.context/study_run/`. Deleting `~/cu-study` removes them.
- You can stop at any time; just do not send the file.
