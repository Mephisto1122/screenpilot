# Screenpilot

An AI agent with its own computer. Give it a long job: it plans, works on a Linux desktop through the screen, mouse and keyboard, and comes back with the result.

![Python](https://img.shields.io/badge/Python-151514?style=flat-square&logo=python&logoColor=white) ![Docker](https://img.shields.io/badge/Docker-151514?style=flat-square&logo=docker&logoColor=white) ![Debian](https://img.shields.io/badge/Debian-151514?style=flat-square&logo=debian&logoColor=white) ![Chromium](https://img.shields.io/badge/Chromium-151514?style=flat-square&logo=googlechrome&logoColor=white) ![xdotool](https://img.shields.io/badge/xdotool-151514?style=flat-square) ![Tesseract OCR](https://img.shields.io/badge/Tesseract_OCR-151514?style=flat-square) ![OpenCV](https://img.shields.io/badge/OpenCV-151514?style=flat-square&logo=opencv&logoColor=white) ![SQLite](https://img.shields.io/badge/SQLite-151514?style=flat-square&logo=sqlite&logoColor=white) ![JavaScript](https://img.shields.io/badge/JavaScript-151514?style=flat-square&logo=javascript&logoColor=white) ![Ollama](https://img.shields.io/badge/Ollama-151514?style=flat-square&logo=ollama&logoColor=white) ![UI-TARS](https://img.shields.io/badge/UI--TARS-151514?style=flat-square) ![Qwen3-VL](https://img.shields.io/badge/Qwen3--VL-151514?style=flat-square) ![Holo](https://img.shields.io/badge/Holo-151514?style=flat-square) ![Claude](https://img.shields.io/badge/Claude-151514?style=flat-square&logo=anthropic&logoColor=white) ![GPT](https://img.shields.io/badge/GPT-151514?style=flat-square&logo=openai&logoColor=white) ![Telegram](https://img.shields.io/badge/Telegram-151514?style=flat-square&logo=telegram&logoColor=white)

![Screenpilot working on a task: the plan in the chat on the left, its desktop on the right](docs/working.png)

**[Watch the 70-second overview](docs/screenpilot.mp4)**

## What it does

- **Plans first.** It says what you will get, what it decided, and what proves each step is finished.
- **Works alone.** It stops only for logins, codes and approvals.
- **Talks while it works.** Ask a question, correct it, or rework the result in the chat.
- **Knows how.** Built-in playbooks for searching, finding people and companies, and drawing conclusions.
- **Runs on your machine.** In Docker, with local models, or with Claude or GPT.

## Start

Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) and [Ollama](https://ollama.com/download), then:

```bash
./run.sh
```

Open **http://localhost:8080** and type a task.

## Modes

| Mode | Where the thinking happens |
|---|---|
| **Private** | On your Mac, with local models. Nothing leaves it. |
| **Smart & private** | A cloud model decides from a text description. Screenshots stay on your Mac. |
| **Fastest** | A cloud model (Claude or GPT) sees the screen. |

## More

The [guide](docs/GUIDE.md) covers setup options, approvals and accounts, Telegram, costs, know-how, troubleshooting, security and tests.

<sub>The screenshot and the video show a sample task with made-up companies, run in the built-in test mode with a scripted model. The dark pane is the test screen; in normal use it shows the live Linux desktop.</sub>
