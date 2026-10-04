### 🗺️ Database schema changes

**4 changes across 3 tables** · 🔴 1 breaking · 🟢 3 safe

> [!WARNING]
> 1 change can break running code or lose data. Check the deploy order (expand → migrate → contract).

| | Change | Detail |
| --- | --- | --- |
| 🟢 | Table `audit_events` added |  |
| 🔴 | Column `organizations.plan` removed | was text not null default 'free'<br>_its data is dropped and queries using it fail_ |
| 🟢 | Column `tasks.due_on` added | date |
| 🟢 | Column `tasks.priority` added | smallint not null default 2 |

<details open><summary>Diagram of the changed tables</summary>

```mermaid
erDiagram
    audit_events {
        bigint id PK "🟢 new table"
        bigint organization_id FK "🟢 new table"
        bigint actor_id FK "🟢 new table"
        text action "🟢 new table"
        jsonb payload "🟢 new table"
        timestamptz created_at "🟢 new table"
    }
    organizations {
        bigint id PK
        varchar(64) slug UK
        text name
        timestamptz created_at
        text plan "🔴 removed"
    }
    tasks {
        bigint id PK
        bigint project_id FK
        text title
        task_status status
        bigint assignee_id FK
        bigint created_by FK
        timestamptz created_at
        timestamptz updated_at
        date due_on "🟢 added"
        smallint priority "🟢 added"
    }
    comments
    memberships
    projects
    subscriptions
    users
    users |o..o{ audit_events : "actor_id"
    organizations ||..o{ audit_events : "organization_id"
    organizations ||--o{ memberships : "organization_id"
    organizations ||..o{ projects : "organization_id"
    organizations ||..o| subscriptions : "organization_id"
    users |o..o{ tasks : "assignee_id"
    users ||..o{ tasks : "created_by"
    projects ||..o{ tasks : "project_id"
    tasks ||..o{ comments : "task_id"
```

🟢 added · 🔴 removed · 🟡 changed · unmarked columns are unchanged

</details>

<sub>Compared migrations through `V3__billing.sql` with the result of adding `V4__due_dates_and_audit.sql`, by diagram-regenerator.</sub>
