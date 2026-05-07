# TODO App — Design Document

## Goal

A single-process Python web app for managing a personal todo list. Single-user, no auth, runs locally on http://127.0.0.1:5000.

## User stories

- I can see all my todos on the home page.
- I can add a new todo via a form.
- I can mark a todo as done.
- I can delete a todo.

## Functional requirements

1. **Web UI** at GET `/` — renders list of todos with checkbox + delete button per item, plus a "new todo" form at the top.
2. **REST API** at:
   - `GET /api/todos` — JSON array of `{id, text, done}`.
   - `POST /api/todos` — body `{text}`, returns the created todo.
   - `PATCH /api/todos/<id>` — body `{done: bool}`, returns updated todo.
   - `DELETE /api/todos/<id>` — returns `{ok: true}`.
3. **Persistence** in SQLite at `./todos.db`, schema `todos(id INTEGER PK, text TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0)`.

## Non-functional requirements

- Python 3.12, Flask 3.x.
- Single-file deployable: `python app.py` starts the server.
- All runtime AND test dependencies in `requirements.txt` (Flask, pytest, ...; the smoke harness installs them via `uv pip install -r requirements.txt`). The Generator must include `pytest` because acceptance criteria below depend on running it.
- Tests in `tests/` covering each endpoint (pytest + Flask test client).

## Acceptance criteria (the harness will verify these)

- The home page renders without HTTP 500.
- POSTing a todo via the form makes it appear on the list after the redirect.
- The DELETE endpoint removes the row from SQLite.
- `pytest` from the project root passes with zero failures.
