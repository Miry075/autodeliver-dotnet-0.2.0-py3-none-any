Stage: PR TEXT (read-only). Write the pull request title and summary for a human reviewer.

Work item AB#{{work_item_id}}: {{title}}
Acceptance criteria: {{acceptance_criteria}}
Plan summary: {{plan_summary}}
Gate results (already verified by code; do not restate them as your own claims):
{{gate_table}}

Rules: the title is imperative and 80 characters or fewer, with no work item id (the pipeline adds it). The summary is markdown, 120 words or fewer: what changed and why, and what the reviewer should look at first. Make no claims beyond the inputs. Include no personal data.
