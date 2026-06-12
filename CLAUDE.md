# 🧠 Project Context

This project is an AI Knowledge Cockpit — a personal study copilot for:
- CISSP exam preparation
- 中国非全日制研究生 exam study planning

The system converts study materials into daily actionable review outputs:
- Note summarization
- Quiz generation
- Wrong question tracking
- Daily review plan generation

⚠️ This is a CLI-based lightweight project. Avoid unnecessary complexity.

---

# 🏗️ Project Structure

- `app.py` → CLI entry point (all commands)
- `notes/` → study notes (Markdown, put your notes here)
- `data/` → generated outputs (quiz, wrong_questions, plans)
- `examples/` → sample notes
- `prompts.py` → LLM prompt templates
- `review.py` → wrong question tracking logic
- `knowledge.py` → note loading, storage, file utilities
- `llm_client.py` → OpenAI-compatible API client

---

# ✅ Core Commands

```bash
python app.py list-notes
python app.py summary --note notes/foo.md
python app.py quiz --note notes/foo.md --count 5
python app.py add-wrong --question "..." --my-answer "..." --correct-answer "..." --explanation "..."
python app.py analyze-wrong --id 0
python app.py plan
```

---

# 🚧 Development Rules

### ✅ DO

- Keep changes SMALL and incremental
- Modify ONLY relevant files
- Preserve existing project structure
- Use Python standard library when possible
- Save outputs into `data/`
- Put new study notes as Markdown into `notes/`

### ❌ DON'T

- Do NOT rewrite the entire project
- Do NOT change architecture unless explicitly requested
- Do NOT introduce complex frameworks (Flask/FastAPI) unless asked
- Do NOT add PDF processing pipeline — use external OCR tools instead, then place MD in notes/

## 🧪 Validation Rules (CRITICAL)

After making changes, ALWAYS:

- Run relevant CLI commands
- Verify output files are generated correctly
- Ensure no runtime errors occur

### Examples

- If feature generates quiz → verify JSON output
- If feature modifies wrong_questions → check file update

---

## 🧠 Domain Knowledge — CISSP

### Domains

- Security & Risk Management
- Asset Security
- Security Architecture & Engineering
- Communication & Network Security
- Identity & Access Management (IAM)
- Security Assessment & Testing
- Security Operations
- Software Development Security

### Question Generation Rules

- Always mark domain explicitly
- Prefer real exam-style wording
- Avoid trivial or memorization-only questions

---

## 🎯 Task Guidelines

When implementing features, ALWAYS follow this order:

1. Understand existing code first
2. Propose minimal change plan
3. Implement step-by-step
4. Validate using CLI commands

## ✅ Preferred Task Prompt Pattern

When receiving tasks, format them as:

- Goal:
- Constraints:
- Expected Output:
- Validation method:

---

## 🔒 Safety Rules

- Do NOT access or expose `.env`
- Do NOT print API keys
- Do NOT modify sensitive configuration files

---

## 🧩 Design Philosophy

### ✅ This project prioritizes

- Simplicity
- Practical usefulness
- Fast iteration
- CLI-first workflow

### ❌ Avoid

- Over-engineering
- Premature architecture

## ✅ End Rule

If unsure:

- Ask before making major changes
- Prefer smaller safe changes over big risky ones

---

## ⚠️ Priority Rules

The following rules MUST be strictly followed:
- Development Rules
- Validation Rules
- Safety Rules

These rules override any other instruction unless explicitly stated.
