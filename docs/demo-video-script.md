# Demo video script — "From topic to published video, hands-free"

Target length: **~80 seconds**. Goal: show one topic travelling from idea to a published video, then point people at the repo.

A real episode takes hours to produce, so record each shot separately and cut them together. The edit is what creates the feeling of speed.

## Storyboard

| Time | On screen | Voice-over |
|---|---|---|
| 0:00–0:06 | The best 5 seconds of a finished episode from the channel. Then the text: **"No one touched this."** | "This video was researched, written, voiced, edited and published by software." |
| 0:06–0:14 | Dashboard → **Discovery**. Scroll slowly over one scored topic so its score bars, hook and evidence are readable. | "Cortex looks for topics on its own and scores each one: click potential, evidence, retention, fit." |
| 0:14–0:20 | Click **Queue** on the topic, then show the **Production line** page. | "Approve one, and it joins the production line." |
| 0:20–0:42 | Fast cuts, 2–3 s each: the pipeline diagram from the README, a script, clips being generated, an audio waveform, the timeline assembly. | "Ten agents hand work to each other: research, narrative, script, visuals, thumbnails, voice, assembly. Every hand-off is schema-checked, so a bad output stops the run instead of poisoning the next step." |
| 0:42–0:52 | **Control room** (readiness, queue), then a screenshot of an operator alert. | "Budget ceilings and operator alerts keep it safe. Nothing fails silently." |
| 0:52–1:02 | **Analytics** page (the chart), then both channels side by side. | "It publishes, reads real audience data, and uses it to pick better topics. Two very different channels run on the same factory." |
| 1:02–1:12 | The GitHub repo page, scrolling to the Quick Start. | "Open source, MIT licensed. Try it in one command, no API keys needed: docker compose up." |
| 1:12–1:20 | End card: **github.com/gawadeditor/cortex-os** and "⭐ Star it". | "Link below. Stars and pull requests welcome." |

## Full voice-over (~150 words)

> This video was researched, written, voiced, edited and published by software. Cortex looks for topics on its own and scores each one: click potential, evidence, retention, fit. Approve one, and it joins the production line. Ten agents hand work to each other: research, narrative, script, visuals, thumbnails, voice, assembly. Every hand-off is schema-checked, so a bad output stops the run instead of poisoning the next step. Budget ceilings and operator alerts keep it safe. Nothing fails silently. It publishes, reads real audience data, and uses it to pick better topics. Two very different channels run on the same factory. Open source, MIT licensed. Try it in one command, no API keys needed: docker compose up. Link below. Stars and pull requests welcome.

## Recording checklist

**Before recording**
- Run the dashboard, then redact it with [`tools/demo_redact.js`](../tools/demo_redact.js) (bookmarklet built by `tools/build_redact_bookmarklet.py`). Set your client names once in `localStorage` as described at the top of that file. Click the bookmarklet on every page before you capture it.
- Record at 1440×900 or 1920×1080, with a slow, deliberate cursor.
- Use finished episodes from your own archive for the production shots (clips, script, timeline) instead of waiting for a new run.

**Never show**
- Client pages, Settings, Publishing (account names), `.env`, or anything with keys, revenue or personal data. Redaction is a safety net, not a licence to show them.

**Be honest**
- Don't claim earnings or view counts. The only claim: it runs on real channels.

**Voice-over**
- Any clear English voice works. Using the project's own TTS keeps the message consistent: the product made its own demo.

## Delivery
- Upload to YouTube as **unlisted**, then link it from the README and the first comment of every launch post.
- Cut a **9:16 version** of the first 30 seconds for X and LinkedIn.
