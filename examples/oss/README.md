# Demo: three open-source apps

Diagram Regenerator run, unchanged, on the real migration histories of three MIT-licensed
open-source projects. Nothing was configured beyond pointing at the migrations folder and,
for the two larger schemas, naming a few diagram groups.

| App | Migrations | Result | Newest structural change |
| --- | --- | --- | --- |
| [Umami](umami/README.md): web analytics | 26 Prisma migrations | 26 tables, 229 columns, 92 indexes | [`26_add_api_key`](umami/latest-migration.md): 🟢 table added |
| [Atuin](atuin/README.md): shell-history sync server | 22 sqlx migrations | 5 tables, 38 columns, 9 indexes | [`drop-history-count-trigger`](atuin/latest-migration.md): 🔴 table dropped |
| [Hoppscotch](hoppscotch/README.md): API development platform | 22 Prisma migrations | 23 tables, 171 columns, 22 foreign keys | [`published_doc_environment`](hoppscotch/latest-migration.md): 🟢 3 columns added |

Each folder holds the generated `README.md` (diagrams and data dictionary, rendered by GitHub),
the `schema.json` snapshot, and `latest-migration.md`: the comment a pull request adding
that app's newest table-changing migration would have received.

## What the runs showed

- **All migrations replay without warnings.** Prisma, sqlx and plain PostgreSQL DDL,
  including functions, triggers, enum swaps and data backfills (skipped, since they don't
  change tables).
- **The result is exact.** Umami's rebuilt schema was checked against its own
  `schema.prisma`: 26 of 26 models and 229 of 229 fields match.
- **The docs show the database as it is.** Umami uses Prisma's `relationMode = "prisma"`,
  which keeps relations in application code, so its database has a single real foreign key
  and its diagram draws a single relationship. Hoppscotch's 22 foreign keys all appear.
- **Risk ratings match intent.** Atuin's newest migration drops an unused counter table: the
  comment flags it 🔴 breaking because its data is lost, which is exactly what a reviewer
  should confirm is intended.
- **Building it surfaced two parser gaps,** both now fixed: Prisma's `DROP COLUMN …, ADD
  COLUMN …` in one statement, and `SET DATA TYPE … USING … AT TIME ZONE` (Hoppscotch).

## Reproduce

```bash
pip install -e .                 # from the repository root
python examples/oss/build.py     # or: python examples/oss/build.py umami
```

The script fetches only each app's migrations folder at a pinned commit (a sparse, shallow
clone into `examples/oss/.cache`, which git ignores) and regenerates everything above.

| App | Repository | Pinned commit | Licence |
| --- | --- | --- | --- |
| Umami | [umami-software/umami](https://github.com/umami-software/umami) | `ec0ff50` | MIT |
| Atuin | [atuinsh/atuin](https://github.com/atuinsh/atuin) | `d440a2e` | MIT |
| Hoppscotch | [hoppscotch/hoppscotch](https://github.com/hoppscotch/hoppscotch) | `63273f8` | MIT |

The schemas belong to their projects; these pages are generated from their public
migrations for demonstration only and are not affiliated with or endorsed by them.
