"""Apply a ProjectTemplate folder tree + optional review workflow to a project."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models import Folder, Project, ProjectTemplate, ReviewWorkflowStage, ReviewWorkflowTemplate


def apply_project_template(
    db: Session,
    project: Project,
    template: ProjectTemplate,
    created_by_user_id: int,
) -> None:
    definition = template.definition or {}
    folders = definition.get("folders") or []

    def create_nodes(nodes: list, parent_id: int | None) -> None:
        for node in nodes:
            if not isinstance(node, dict):
                continue
            name = (node.get("name") or "").strip() or "Untitled"
            f = Folder(
                project_id=project.id,
                parent_id=parent_id,
                name=name,
                created_by=created_by_user_id,
            )
            db.add(f)
            db.flush()
            children = node.get("children") or []
            if isinstance(children, list):
                create_nodes(children, f.id)

    create_nodes(folders, None)

    wf = definition.get("workflow_template")
    if isinstance(wf, dict):
        stages = wf.get("stages") or []
        if stages:
            t = ReviewWorkflowTemplate(
                project_id=project.id,
                name=(wf.get("name") or "Review workflow").strip() or "Review workflow",
            )
            db.add(t)
            db.flush()
            for idx, st in enumerate(stages):
                if not isinstance(st, dict):
                    continue
                db.add(
                    ReviewWorkflowStage(
                        template_id=t.id,
                        stage_index=idx,
                        stage_key=(st.get("stage_key") or f"stage_{idx}").strip() or f"stage_{idx}",
                        label=(st.get("label") or st.get("stage_key") or f"Stage {idx}").strip(),
                        notify_user_ids=list(st.get("notify_user_ids") or []),
                    )
                )
