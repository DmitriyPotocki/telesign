"""The main pipeline: orchestrator -> validation -> generation -> judging -> delivery.

Keeps per-run DraftStreamer usage and per-chat queue in the handler layer.
"""
from __future__ import annotations

import json
import logging
import pathlib
import secrets
import time

from aiogram import Bot
from aiogram.types import (
    BufferedInputFile,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from ..config import Config
from ..db.repo import Database
from ..imaging.generate import ImageClient
from ..imaging.metrics import edge_ssim, resize_image
from ..llm.judge import Judge
from ..llm.openrouter import OpenRouter, image_data_url
from ..llm.orchestrator import Orchestrator, build_orchestrator_messages
from ..llm.prompts import VIEWER_SYNTHESIS_SYSTEM, VIEWER_SYSTEM
from ..llm.schemas import JudgeScores, Plan
from ..services.files import FileStorage
from ..telegram.draft_streamer import DraftStreamer
from ..telegram.quote_streamer import QuoteStreamer

logger = logging.getLogger(__name__)

MAX_REFERENCES = 8  # mold + up to 7 textures/refs (K3 limit)
GEN_MAX_SIDE = 2048    # K3 Edit inputs (mold/textures): user-visible detail
SCORE_MAX_SIDE = 1024  # judge & SSIM inputs: internal only, never shown to the user
MAX_VIEW_FILES = 6  # per brief: only this many uncached files get viewed

RESULT_BUTTONS = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="\U0001f501 Ещё варианты", callback_data="more"),
            InlineKeyboardButton(text="\u21a9\ufe0f Откатить", callback_data="rollback"),
        ],
        [
            InlineKeyboardButton(text="\u25b6\ufe0f Продолжить от этого", callback_data="continue_here"),
        ],
    ]
)

# --- pending base choices (gallery -> user taps a number) ---------------------
PENDING_CHOICE_TTL = 600.0  # seconds before an untapped gallery expires
_PENDING_BASE_CHOICES: dict[str, dict] = {}


def stash_base_choice(brief: str, mapping: dict[int, int]) -> str:
    """Remember a text brief until the user picks the image to edit."""
    now = time.monotonic()
    for token in [
        t for t, v in _PENDING_BASE_CHOICES.items() if now - v["ts"] > PENDING_CHOICE_TTL
    ]:
        _PENDING_BASE_CHOICES.pop(token, None)
    token = secrets.token_hex(4)
    _PENDING_BASE_CHOICES[token] = {"brief": brief, "mapping": mapping, "ts": now}
    return token


def pop_base_choice(token: str) -> dict | None:
    entry = _PENDING_BASE_CHOICES.pop(token, None)
    if entry is None:
        return None
    if time.monotonic() - entry["ts"] > PENDING_CHOICE_TTL:
        return None
    return entry


def peek_base_choice(token: str) -> dict | None:
    """Read without consuming (so a wrong tap does not lose the brief)."""
    entry = _PENDING_BASE_CHOICES.get(token)
    if entry is None:
        return None
    if time.monotonic() - entry["ts"] > PENDING_CHOICE_TTL:
        _PENDING_BASE_CHOICES.pop(token, None)
        return None
    return entry


class Pipeline:
    def __init__(
        self,
        db: Database,
        cfg: Config,
        client: OpenRouter,
        storage: FileStorage,
        streamer: DraftStreamer,
    ) -> None:
        self.db = db
        self.cfg = cfg
        self.client = client
        self.storage = storage
        self.streamer = streamer
        self.orchestrator = Orchestrator(client, cfg)
        self.image_client = ImageClient(client, cfg)
        self.judge = Judge(client, cfg)

    # --- public -----------------------------------------------------------
    async def run(
        self,
        user_id: int,
        chat_id: int,
        bot: Bot,
        brief: str,
        *,
        attached: list[int] | None = None,
        base_override: int | None = None,
        offer_base_choice: bool = False,
    ) -> None:
        """Full step: plan -> generate -> judge -> deliver. Raises on hard failure.

        attached: files sent together with the brief (prefer as base for edits);
        base_override: the user picked this file in the gallery (hard override);
        offer_base_choice: text-only brief -> show the gallery instead of guessing.
        """
        project_id = await self.db.get_active_project(user_id)

        files = [
            {
                "id": f.id,
                "kind": f.kind,
                "caption": f.caption,
                "analysis": f.analysis,
            }
            for f in await self.db.list_files(user_id, limit=50)
        ]
        instructions = [i["text"] for i in await self.db.list_instructions(user_id)]
        steps = await self.db.list_steps(project_id)
        history = [
            {
                "id": s.id,
                "brief_text": s.brief_text,
                "winner_file_id": s.winner_file_id,
            }
            for s in steps
        ]

        # viewer phase: streamed <blockquote> blocks per image + synthesis
        viewer_take = await self._viewer_phase(user_id, chat_id, bot, files, brief)

        await self.streamer.status("Анализирую бриф…")

        attached_desc = None
        if attached:
            kinds = {f["id"]: f["kind"] for f in files}
            attached_desc = ", ".join(f"#{i} (kind={kinds.get(i, '?')})" for i in attached)

        messages = build_orchestrator_messages(
            brief, files, instructions, history, self.cfg.default_variants,
            viewer_notes=[viewer_take] if viewer_take else None,
            attached_files_desc=attached_desc,
        )
        try:
            plan = await self.orchestrator.plan(messages)
        except RuntimeError as e:
            logger.error("orchestrator failed: %s", e)
            await self.streamer.finalize(
                "Не получилось разобрать бриф. Попробуй переформулировать короче и конкретнее."
            )
            return

        # reference count check: mold + up to 7 textures/refs
        plan = await self._validate_refs(user_id, plan)
        if base_override and plan.action == "generate" and plan.jobs:
            # the user explicitly picked this file in the gallery
            plan.jobs[0].base_file_id = base_override
        if plan.action == "ask_clarification":
            await self.streamer.finalize(plan.question or "Уточни, пожалуйста, задачу.")
            return
        if plan.action == "chat":
            await self.streamer.finalize(plan.reply or "Принял.")
            return
        if not plan.jobs:
            await self.streamer.finalize("Не вижу задачи для генерации. Опиши, что сделать.")
            return

        # text-only brief: show the gallery (winner + references) and wait for a tap
        if offer_base_choice and base_override is None:
            gallery = await self._base_gallery(user_id)
            if gallery:
                await self._send_base_gallery(chat_id, bot, brief, gallery)
                return

        # daily image budget
        used_today = await self.db.images_today(user_id)
        planned = sum(j.n_variants for j in plan.jobs)
        if used_today >= self.cfg.daily_image_limit:
            await self.streamer.finalize(
                f"Сегодня лимит картинок исчерпан ({used_today}/{self.cfg.daily_image_limit}). "
                "Продолжим завтра."
            )
            return
        if used_today + planned > self.cfg.daily_image_limit:
            for j in plan.jobs:
                left = self.cfg.daily_image_limit - used_today
                if left <= 0:
                    break
                j.n_variants = min(j.n_variants, left)
                used_today -= j.n_variants

        # > 7 textures -> several sequential passes over surface groups
        job = plan.jobs[0]
        texture_groups = self._split_materials(user_id, job.materials)
        parent_step: int | None = None
        base_file_id = job.base_file_id

        for pass_index, materials in enumerate(texture_groups):
            brief_part = brief if pass_index == 0 else f"{brief} (проход {pass_index + 1})"
            step = await self.db.add_step(project_id, brief_part, None, base_file_id, parent_step)
            plan_json = self._job_plan_json(job, materials)
            await self.db.execute(
                "UPDATE steps SET plan_json=? WHERE id=?", (plan_json, step.id)
            )
            winner_file_id = await self._run_job(
                bot, user_id, chat_id, step, job, materials, base_file_id
            )
            if winner_file_id is None:
                await self.streamer.finalize("Генерация не удалась, все попытки исчерпаны. Попробуй ещё раз.")
                return
            parent_step = step.id
            base_file_id = winner_file_id
            job = self._clone_job_for_next_pass(job)

    # --- viewer phase -------------------------------------------------------
    async def _viewer_phase(
        self, user_id: int, chat_id: int, bot: Bot, files: list[dict], brief: str
    ) -> str:
        """Show 'viewing image' quote blocks + a streamed synthesis block.

        Viewing is cached per file (files.analysis): files seen before are not
        re-viewed, but their notes still reach the orchestrator. Returns the
        synthesis text (also shown to the user) or "" when there is nothing to view.
        """
        has_media = any(f.get("kind") in ("mold", "texture", "reference") for f in files)
        if not has_media:
            return ""
        notes, step_no = await self._view_materials(user_id, chat_id, bot, files)
        return await self._stream_synthesis(chat_id, bot, brief, files, notes, step_no)

    async def _view_materials(
        self, user_id: int, chat_id: int, bot: Bot, files: list[dict]
    ) -> tuple[list[str], int]:
        labels = {"mold": "молд", "texture": "текстура", "reference": "референс"}
        pending = [
            f for f in files
            if f.get("kind") in labels and not f.get("analysis")
        ][:MAX_VIEW_FILES]
        notes: list[str] = []
        step_no = 1
        for f in pending:
            # numbered tool-call style; the model names the image in its reasoning,
            # user captions (often the whole brief) are never used as names
            header = f"🔧 Шаг #{step_no} · смотрю изображение: {labels[f['kind']]} #{f['id']}"
            step_no += 1
            stream = QuoteStreamer(bot, chat_id)
            await stream.start(header + "\n")
            text = await self._view_one(f["kind"], f["id"], user_id, stream)
            await stream.finalize()
            if not text:
                continue
            f["analysis"] = text  # the orchestrator reads the same dicts below
            try:
                await self.db.set_file_analysis(user_id, f["id"], text)
            except Exception:
                logger.exception("analysis save failed (file %s)", f["id"])
            notes.append(f"{labels[f['kind']]} #{f['id']}: {text}")
        return notes, step_no

    async def _view_one(
        self, kind: str, file_id: int, user_id: int, stream: QuoteStreamer
    ) -> str:
        """One vision call streamed into the quote block. Returns the text."""
        data = await self.storage.read_bytes(user_id, file_id)
        if data is None:
            return ""
        try:
            img = resize_image(data, max_side=1024)
        except Exception:
            logger.warning("cannot decode file %s for viewing", file_id)
            return ""
        messages = [
            {"role": "system", "content": VIEWER_SYSTEM},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"role={kind}. Describe this image."},
                    {"type": "image_url", "image_url": {"url": image_data_url(img)}},
                ],
            },
        ]
        parts: list[str] = []
        try:
            async for piece in self.client.chat_stream(
                self.cfg.llm_model,
                messages,
                reasoning={"effort": "low", "exclude": True},
                max_tokens=400,
            ):
                parts.append(piece)
                await stream.feed(piece)
        except Exception as e:
            logger.warning("viewer call failed for file %s: %s", file_id, e)
        return "".join(parts).strip()

    async def _stream_synthesis(
        self, chat_id: int, bot: Bot, brief: str, files: list[dict],
        notes: list[str], step_no: int = 1,
    ) -> str:
        """Stream the 'how I will apply materials' reasoning as its own block."""
        observations = [
            f["analysis"] for f in files
            if f.get("kind") in ("mold", "texture", "reference") and f.get("analysis")
        ]
        if not observations:
            return ""
        stream = QuoteStreamer(bot, chat_id)
        await stream.start(f"🔧 Шаг #{step_no} · анализ\n")
        messages = [
            {"role": "system", "content": VIEWER_SYNTHESIS_SYSTEM},
            {
                "role": "user",
                "content": f"Brief: {brief}\n\nObservations:\n" + "\n".join(observations),
            },
        ]
        parts: list[str] = []
        try:
            async for piece in self.client.chat_stream(
                self.cfg.llm_model,
                messages,
                reasoning={"effort": "low", "exclude": True},
                max_tokens=250,
            ):
                parts.append(piece)
                await stream.feed(piece)
        except Exception as e:
            logger.warning("viewer synthesis failed: %s", e)
        await stream.finalize()
        return "".join(parts).strip()

    # --- base gallery (text-only briefs) -------------------------------------
    async def _base_gallery(self, user_id: int) -> list[dict]:
        """Generated variants of the active step (winner first, then alternatives
        by score). Falls back to the user's uploads only when nothing was
        generated yet — the gallery exists to pick what the bot made."""
        project_id = await self.db.get_active_project(user_id)
        step = await self.db.get_active_step(project_id)
        entries: list[dict] = []

        if step:
            cands = await self.db.list_candidates(step.id)

            def _total(c: dict) -> float:
                try:
                    return float((json.loads(c.get("score_json") or "{}") or {}).get("total") or 0.0)
                except (TypeError, ValueError):
                    return 0.0

            cands.sort(key=lambda c: (not c.get("is_winner"), -_total(c)))
            for c in cands:
                if not pathlib.Path(c["path"]).exists():
                    continue
                label = "текущий результат" if c.get("is_winner") else "альтернатива"
                entries.append(
                    {"file_id": c["file_id"], "label": label, "path": c["path"]}
                )
                if len(entries) >= 9:
                    break
        if entries:
            return entries

        # nothing generated yet: offer the uploads, named by the model, not by captions
        labels = {"mold": "молд", "texture": "текстура", "reference": "референс"}
        for f in await self.db.list_files(user_id, limit=50):
            if f.kind not in labels or not pathlib.Path(f.path).exists():
                continue
            label = labels[f.kind]
            if f.kind in ("texture", "reference") and f.analysis:
                label += f" \u2014 {f.analysis[:40]}"  # the model names its own
            elif f.kind == "mold":
                if f.caption:
                    label += f" \u2014 {f.caption[:40]}"
                elif f.analysis:
                    label += f" \u2014 {f.analysis[:40]}"
            entries.append({"file_id": f.id, "label": label, "path": f.path})
            if len(entries) >= 9:
                break
        return entries

    async def _send_base_gallery(
        self, chat_id: int, bot: Bot, brief: str, entries: list[dict]
    ) -> None:
        """Send the gallery as numbered documents + numbered buttons."""
        mapping = {n: e["file_id"] for n, e in enumerate(entries, 1)}
        token = stash_base_choice(brief, mapping)
        for n, e in enumerate(entries, 1):
            try:
                await bot.send_document(
                    chat_id,
                    FSInputFile(e["path"]),
                    caption=f"\u2116{n} \u2014 {e['label']}",
                )
            except Exception:
                logger.exception("gallery send failed for file %s", e["file_id"])
        buttons = [
            InlineKeyboardButton(text=f"\U0001f5bc \u041f\u0440\u0430\u0432\u0438\u0442\u044c \u2116{n}", callback_data=f"editbase:{token}:{n}")
            for n in mapping
        ]
        rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
        await bot.send_message(
            chat_id,
            "\u041a\u0430\u043a\u043e\u0435 \u0438\u0437\u043e\u0431\u0440\u0430\u0436\u0435\u043d\u0438\u0435 \u043f\u0440\u0430\u0432\u0438\u0442\u044c? \u0422\u0430\u043f\u043d\u0438 \u043d\u043e\u043c\u0435\u0440 \u2014 \u0432\u044b\u043f\u043e\u043b\u043d\u044e \u044d\u0442\u043e\u0442 \u0431\u0440\u0438\u0444 \u043f\u043e \u0432\u044b\u0431\u0440\u0430\u043d\u043d\u043e\u043c\u0443.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )

    # --- helpers ----------------------------------------------------------
    def _job_plan_json(self, job, materials) -> str:
        return json.dumps(
            {
                "action": "generate",
                "base_file_id": job.base_file_id,
                "n_variants": job.n_variants,
                "materials": [m.model_dump() for m in materials],
                "prompt": job.prompt,
                "aspect_ratio": job.aspect_ratio,
            },
            ensure_ascii=False,
        )

    def _clone_job_for_next_pass(self, job):
        import copy

        return copy.deepcopy(job)

    async def _validate_refs(self, user_id: int, plan: Plan) -> Plan:
        """Check that referenced files exist, belong to the user; clamp variants."""
        valid_ids = {f.id for f in await self.db.list_files(user_id, limit=200)}
        for job in plan.jobs:
            if job.base_file_id not in valid_ids:
                job.base_file_id = next((fid for fid in valid_ids), 0) if valid_ids else -1
            mats = []
            for m in job.materials:
                if m.texture_file_id in valid_ids:
                    mats.append(m)
            job.materials = mats
            if not valid_ids and plan.action == "generate":
                plan.action = "ask_clarification"
                plan.question = "Не нашёл загруженных файлов. Пришли сначала молд (фото помещения)."
        return plan

    def _split_materials(self, user_id: int, materials) -> list[list]:
        """Textures > 7 -> group into sequential passes, don't silently drop."""
        if len(materials) <= self.cfg.max_texture_refs:
            return [materials] if materials else [[]]
        groups = []
        for i in range(0, len(materials), self.cfg.max_texture_refs):
            groups.append(materials[i : i + self.cfg.max_texture_refs])
        return groups

    async def _progress_cb(self, bot: Bot, chat_id: int):
        async def cb(done: int, total: int) -> None:
            await self.streamer.status(f"Генерирую {done}/{total}…")

        return cb

    async def _run_job(self, bot, user_id, chat_id, step, job, materials, base_file_id) -> int | None:
        mold_bytes = await self.storage.read_bytes(user_id, base_file_id)
        if mold_bytes is None:
            await self.streamer.finalize("Файл-молд недоступен. Загрузи его заново.")
            return None

        textures: list[bytes] = []
        materials_desc_parts: list[str] = []
        materials_pairs: list[tuple[str, str]] = []
        for m in materials:
            tex_bytes = await self.storage.read_bytes(user_id, m.texture_file_id)
            if tex_bytes is not None:
                # generation input: keep as much detail as the API accepts
                textures.append(resize_image(tex_bytes, max_side=GEN_MAX_SIDE))
                name = str(m.texture_name or m.texture_file_id)
                materials_desc_parts.append(f"{m.surface} <- {name}")  # for the judge
                materials_pairs.append((m.surface, name))  # for build_prompt

        prompt = self.image_client.build_prompt(job.prompt, materials_pairs)

        cb = await self._progress_cb(bot, chat_id)
        variants = await self.image_client.generate_variants(
            prompt,
            resize_image(mold_bytes, max_side=GEN_MAX_SIDE),
            textures,
            job.n_variants,
            aspect_ratio=job.aspect_ratio,
            progress_cb=cb,
        )
        good = [(i, img) for i, img in variants if img is not None]
        if not good:
            return None

        await self.streamer.status("Выбираю лучший…")

        # programmatic geometry metric
        mold_small = resize_image(mold_bytes, max_side=SCORE_MAX_SIDE)
        cand_scores: list[float] = []
        for _i, img in good:
            metric = edge_ssim(mold_small, resize_image(img, max_side=SCORE_MAX_SIDE))
            cand_scores.append(metric)

        # judge via vision
        scores: list[JudgeScores | None] = []
        for _i, img in good:
            js = None
            try:
                js = await self.judge.score(
                    resize_image(mold_bytes, max_side=SCORE_MAX_SIDE),
                    list(zip(materials_desc_parts, textures, strict=False)) if materials_desc_parts else [],
                    resize_image(img, max_side=SCORE_MAX_SIDE),
                    "; ".join(materials_desc_parts),
                )
            except Exception as e:
                logger.warning("judge failed: %s", e)
            scores.append(js)

        def total(idx: int) -> float:
            metric = cand_scores[idx]
            js = scores[idx]
            rubric = (
                (js.geometry + js.textures + js.lighting + js.artifacts) / 4.0 if js else 0.0
            )
            return self.cfg.judge_weight_rubric * rubric + self.cfg.judge_weight_metric * metric * 10.0

        totals = [total(idx) for idx in range(len(good))]
        best_idx = max(range(len(good)), key=lambda i: totals[i])
        best_total = totals[best_idx]

        low_quality = best_total < self.cfg.quality_threshold

        # store candidates
        saved: list[tuple[int, float]] = []
        winner_file_id: int | None = None
        for rank, (idx, img) in enumerate(good):
            file_id, _kind = await self.storage.store_bytes(
                user_id,
                img,
                "image/png",
                "result",
                caption=f"Вариант {idx + 1}, шаг {step.id}",
            )
            is_winner = rank == best_idx
            await self.db.add_candidate(
                step.id, file_id, score_json=json.dumps(
                    {
                        "rubric": scores[idx].model_dump() if scores[idx] else None,
                        "edge_ssim": round(cand_scores[idx], 4),
                        "total": round(totals[idx], 3),
                    },
                    ensure_ascii=False,
                ),
                is_winner=is_winner,
            )
            saved.append((file_id, totals[idx]))
            if is_winner:
                winner_file_id = file_id

        # auto-regenerate once if everything is below threshold
        regen_images = 0
        if low_quality:
            logger.info("low quality (%.2f), regenerating once", best_total)
            await self.streamer.status("Качество так себе — перегенерирую…")
            fixed_prompt = prompt + (
                "\nIMPORTANT: previous attempts failed to preserve the mold geometry or "
                "materials looked wrong. Be extremely careful to keep the geometry, "
                "perspective and composition of reference 1 and to apply textures exactly."
            )
            variants2 = await self.image_client.generate_variants(
                fixed_prompt,
                resize_image(mold_bytes, max_side=GEN_MAX_SIDE),
                textures,
                min(job.n_variants, 4),
                aspect_ratio=job.aspect_ratio,
            )
            good2 = [(i, img) for i, img in variants2 if img is not None]
            regen_images = len(variants2)
            if good2:
                await self.streamer.status("Выбираю лучший…")
                cand_scores2: list[float] = []
                for _i, img in good2:
                    cand_scores2.append(edge_ssim(mold_small, resize_image(img, max_side=SCORE_MAX_SIDE)))
                scores2: list[JudgeScores | None] = []
                for _i, img in good2:
                    try:
                        scores2.append(
                            await self.judge.score(
                                resize_image(mold_bytes, max_side=SCORE_MAX_SIDE),
                                list(zip(materials_desc_parts, textures, strict=False)),
                                resize_image(img, max_side=SCORE_MAX_SIDE),
                                "; ".join(materials_desc_parts),
                            )
                        )
                    except Exception:
                        scores2.append(None)

                def total2(idx: int) -> float:
                    metric = cand_scores2[idx]
                    js = scores2[idx]
                    rubric = (
                        (js.geometry + js.textures + js.lighting + js.artifacts) / 4.0
                        if js
                        else 0.0
                    )
                    return self.cfg.judge_weight_rubric * rubric + self.cfg.judge_weight_metric * metric * 10.0

                totals2 = [total2(i) for i in range(len(good2))]
                best2 = max(range(len(good2)), key=lambda i: totals2[i])
                if totals2[best2] > best_total:
                    # replace winner
                    img2 = good2[best2][1]
                    file_id, _ = await self.storage.store_bytes(
                        user_id, img2, "image/png", "result", caption=f"Вариант (переген), шаг {step.id}"
                    )
                    await self.db.add_candidate(
                        step.id, file_id,
                        score_json=json.dumps({"regenerated": True, "total": round(totals2[best2], 3)}),
                        is_winner=True,
                    )
                    winner_file_id = file_id
                    best_total = totals2[best2]
                    low_quality = best_total < self.cfg.quality_threshold

        # disputed: top-2 scores close together -> flag for future stronger model
        sorted_totals = sorted(totals, reverse=True)
        disputed = len(sorted_totals) >= 2 and (sorted_totals[0] - sorted_totals[1]) < 0.75

        caption_text = await self._winner_caption(step.id, job, materials, low_quality)
        await self.db.finish_step(
            step.id,
            winner_file_id,
            quality_note=int(low_quality),
            disputed=int(disputed),
        )
        if winner_file_id:
            await self.db.set_file_caption(user_id, winner_file_id, caption_text)

        # usage accounting
        await self.db.log_usage(
            user_id, "image", self.cfg.image_model, images=len(good) + regen_images
        )

        # deliver: documents everywhere (Telegram recompresses send_photo hard)
        winner_bytes = (
            await self.storage.read_bytes(user_id, winner_file_id) if winner_file_id else None
        )
        caption = caption_text
        if winner_bytes:
            try:
                await bot.send_document(
                    chat_id=chat_id,
                    document=BufferedInputFile(winner_bytes, filename=f"step{step.id}_winner.png"),
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=RESULT_BUTTONS,
                )
            except Exception as e:
                logger.error("send winner failed: %s", e)
                await self.streamer.finalize("Готово, но не смог отправить картинку. Смотри /history.")
            else:
                await self._send_other_variants(
                    bot, user_id, chat_id, step.id, saved, winner_file_id
                )
        else:
            await self.streamer.finalize("Готово, но файл победителя потерялся. Смотри /history.")
        return winner_file_id

    async def _send_other_variants(
        self, bot, user_id: int, chat_id: int, step_id: int,
        saved: list[tuple[int, float]], winner_file_id: int | None,
    ) -> None:
        """All remaining candidates as numbered documents (winner is №1)."""
        others = sorted(
            (item for item in saved if item[0] != winner_file_id),
            key=lambda t: -t[1],
        )
        for pos, (fid, _total) in enumerate(others, start=2):
            data = await self.storage.read_bytes(user_id, fid)
            if not data:
                continue
            try:
                await bot.send_document(
                    chat_id,
                    BufferedInputFile(data, filename=f"step{step_id}_var{pos}.png"),
                    caption=f"Вариант №{pos} · шаг {step_id}",
                )
            except Exception:
                logger.warning("failed to send variant %s", pos, exc_info=True)

    async def _winner_caption(self, step_id: int, job, materials, low_quality: bool) -> str:
        mats = ", ".join(f"{m.surface}: {m.texture_name or 'текстура'}" for m in materials) or "—"
        what = (job.prompt or "").strip().split(".")[0][:160]
        parts = [f"Шаг {step_id}: {what}."]
        if materials:
            parts.append(f"Материалы: {mats}.")
        if low_quality:
            parts.append("Качество так себе — попробуй «Ещё варианты».")
        return "\n".join(parts)[:1024]
