# AI Orchestration Rules

The root agent is a low-cost dispatcher.

## Routing
- Simple questions: answer directly.
- File searches and logs: delegate to explorer.
- Complex planning: delegate to planner.
- Approved implementation: delegate to developer.
- Completed implementation: delegate to reviewer.
- Final summaries: delegate to reporter when useful.

## Workflow
1. Explorer collects relevant context.
2. Planner creates an implementation plan with risks, dependencies and acceptance criteria.
3. Ask the user to approve the plan before implementation. Reuse explicit approval already given for that plan; do not ask for it again.
4. Developer implements the approved tasks.
5. Run relevant automated tests and required project checks; record actual results and blockers.
6. Reviewer evaluates the changes and returns PASS or FAIL.
7. On FAIL, return actionable findings to developer and review the fixes again.
8. Reporter summarizes verified results when useful; the root agent owns the final response.

Use subagents for delegated work, not new user-facing chats. Give each developer an explicit file scope; do not run overlapping edits concurrently. Read-only roles must not edit files. Each agent must follow applicable AGENTS.md and .ai context, preserve unrelated work and protect secrets. Parent permissions and runtime concurrency limits still apply. File instructions do not create a separate read-only sandbox or change the running chat's model.

# Контекст репозитория vk-bot

В начале сессии прочитай [.ai/PROJECT.md](.ai/PROJECT.md), [.ai/CURRENT_TASK.md](.ai/CURRENT_TASK.md), [.ai/STATE.md](.ai/STATE.md) и [.ai/CONVENTIONS.md](.ai/CONVENTIONS.md). Карта кода — [.ai/ARCHITECTURE.md](.ai/ARCHITECTURE.md), история — [.ai/CHANGELOG.md](.ai/CHANGELOG.md).

Перед правками проверь ветку, HEAD и рабочее дерево. Сохраняй чужие изменения. После задачи обнови локальные CURRENT_TASK и STATE; архитектуру, правила и CHANGELOG — если они изменились. Записывай факты и реальные проверки, отдельно отмечай непроверенное. Не копируй секреты и персональные данные.

Сообщества ВК подключаются из Bot CRM через `/api/v1/channels/vk*` (ключи шифруются `CHANNEL_TOKEN_KEY`, см. README); отключённое (`channels.enabled = FALSE`) не получает рассылки и уведомления.

Центральный сервис анонимного чата: общая очередь, пары, события, медиа, посетители и статистика. Включает мост к внешним собеседникам.

Локальный комплект работает и при отдельном checkout. Если рядом есть общая папка workspace `../.ai/`, используй её для межсервисного контекста и обновляй при изменении общих контрактов. Её отсутствие не блокирует локальную работу.
