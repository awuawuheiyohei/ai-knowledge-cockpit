# AI Knowledge Cockpit

A personal study copilot for **CISSP** and **中国非全日制研究生** exam prep.
Converts study materials into daily actionable review outputs.

## Quick Start

### 1) Create virtual environment
```bash
python -m venv .venv
source .venv/bin/activate  # Linux/macOS
pip install -r requirements.txt
```

### 2) Configure environment
Create or edit `.env`:
```bash
OPENAI_API_KEY=your_key_here
OPENAI_BASE_URL=https://open.bigmodel.cn/api/paas/v4/
OPENAI_MODEL=glm-5.1
```

### 3) Add your notes
Place Markdown study notes in `notes/`. For example:
```
notes/第1章-实现安全治理的原则和策略-知识点.md
notes/第6章-密码学和对称密钥算法-知识点.md
```

### 4) Study tools
```bash
python app.py list-notes                    # List all notes
python app.py summary --note notes/foo.md   # Generate summary
python app.py quiz --note notes/foo.md -c 5 # Generate quiz questions
python app.py add-wrong --question "..." --my-answer "..." --correct-answer "..." --explanation "..."
python app.py analyze-wrong --id 0          # Analyze wrong question by index
python app.py plan                          # Generate daily review plan
```

## Project Structure

```
├── app.py              # CLI entry point
├── llm_client.py       # OpenAI-compatible API client
├── prompts.py          # Prompt templates
├── knowledge.py        # Note loading, storage, slugify
├── review.py           # Wrong-question & progress tracking
├── requirements.txt    # Python dependencies
├── .env                # API credentials (not committed)
├── .gitignore
├── notes/              # Your study notes (Markdown)
├── data/               # Generated outputs (quiz, plans, wrong questions)
└── examples/           # Sample notes
```
