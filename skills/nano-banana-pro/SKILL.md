---
name: nano-banana-pro
description: Generate or edit images (portraits, product shots, banners, illustrations, mockups, visual assets) and generate short VIDEOS (Veo, with --video) with Nano Banana Pro through the user's own Gemini web session. Use it when the user asks to create, generate, edit or refine an image, to create a video or animated clip, or when a project needs a visual asset that code or SVG cannot produce. The agent writes the prompt, runs the script, looks at the result and iterates.
---

# Nano Banana Pro (Gemini web, driven by an AI agent)

You do not generate pixels. This skill makes you the **art director**: turn the request into a strong prompt, run the script, **look at the image**, and decide whether to redo it.

The script is `scripts/nanobanana.py` inside this skill's base directory. **Run it yourself, directly, from the shell.** Do not delegate it to a sub-agent: it is one command, and a sub-agent only adds tokens. For long runs, run it in the background if your tool supports that, and wait for it to finish instead of polling.

## Workflow

1. **Understand the request.** If something that changes the result is missing (final use, aspect ratio, style, text inside the image, references), ask first. Each image spends daily credits.
2. **Write the prompt in English**, concrete: subject, medium/style, composition, lighting, palette (hex if the project has one), and what you do NOT want. Put text that must appear in the image inside quotes, and keep it short.
3. **Run the script.**
   ```bash
   python -I "<skill-dir>/scripts/nanobanana.py" "<prompt>" -o out/name.png --aspect 3:4
   python -I "<skill-dir>/scripts/nanobanana.py" --prompt-file spec.json -o out/name.png --aspect 4:5 --ref ref1.png --ref ref2.png
   ```
   For complex scenes write the prompt as JSON in a file and use `--prompt-file`; quotes break on the command line.
4. **Look at the result yourself.** Open the saved file with a tool that can show you images (for example your file-read tool). Judge composition, readable text, artifacts, hands, and how well it matches the request. Be honest with the user about what is wrong.
5. **Iterate 2-3 times at most**, adjusting the prompt with what was missing. For a small fix, pass the previous image with `--ref` and ask only for the change. Then show the user and ask.
6. Save assets inside the project (`assets/`, `docs/`), never loose in the project root.

## Videos (`--video`)

**This skill also generates videos.** The same Gemini session produces short clips with Veo; the script just has to be told to ask for a video and to read `response.videos` instead of images. Without `--video` it always asks for an image and will never return a video.

```bash
python -I "<skill-dir>/scripts/nanobanana.py" "a banana slowly rotating on a wooden table, soft studio light" -o out/banana.mp4 --video --aspect 16:9
```

- Output: `SAVED <path>.mp4` and `THUMB <path>.jpg`. **Look at the thumbnail with Read** to judge the clip (you cannot watch the mp4); tell the user you only checked the thumbnail.
- Write the prompt like a shot description: subject, motion, camera move, lighting, duration ("5 seconds"). Describe the motion in words; a still `--ref` image can set the first frame.
- Videos are slower (default timeout 600s) and Google often answers with a generic error or "at full capacity, high traffic generating videos" instead of a video. The script retries on its own (`--retries`, default 2, waiting 10s and alternating `gemini-carbon` / `gemini-flash-lite`). If it still fails (exit 1), wait a few minutes and try again; do not loop.
- Tested 2026-11-17 on an AI Pro account: `gemini-carbon` and `gemini-pro` failed on the first tries (error / empty reply), `gemini-flash-lite` returned a 16:9 MP4 (~3 MB, 5 s). One sample only, so the model difference is not proven; failures looked like Google-side instability, not a plan limit.
- Video spends the same daily credits and is probably costlier than an image: run `--quota` before more than one or two clips.

## Reading the script output

The script prints plain lines. React to them:

| Line | Meaning | What to do |
|---|---|---|
| `SAVED <path>` | image (or .mp4 with `--video`) saved | open an image with Read |
| `THUMB <path>` | video thumbnail (only with `--video`) | open it with Read to judge the clip |
| `RETRY n/m` (stderr) | Google returned no video, trying again | nothing, just wait |
| `MODEL <name>` | model that was used | mention it if the user compares models |
| `QUOTA <n>% left ...` | a threshold was crossed (50%, 25%, 10%, 5% left) | **tell the user in one short sentence**, then continue |
| `LIMIT_REACHED ...` | daily credits are over (exit 4) | stop, tell the user when it resets, do not retry |

Exit codes: `0` ok, `1` no image or refused (read the message), `2` authentication (cookies expired or missing), `3` missing dependency, `4` usage limit reached, `5` timeout. On `2` or `3`, bring the problem to the user instead of retrying.

Before a large batch (more than ~6 images), run `python -I <skill-dir>/scripts/nanobanana.py --quota` and tell the user how many credits are left. It only reads the account and generates nothing. A timeout (exit 5) usually means the limit is over, so check `--quota`.

## Which model

Models on a typical AI Pro account: `gemini-carbon`, `gemini-pro`, `gemini-flash`, `gemini-flash-lite`. The default is `gemini-carbon`; pass `--model` to change it.

- **`gemini-carbon` (default)**: follows geometry, perspective, pose, hand gestures, object positions and fine details of a reference much better. Use it for anything with a camera angle, a pose or a reference.
- **`gemini-flash-lite`**: wins on skin and material finish (glazed ceramic, film grain, glossy retouch), but it simplifies the scene and often drops details such as a hand gesture. Use it for texture-led images.
- For an important image, generate with both and pick one.

## Prompt lessons

- **JSON prompts work better** for complex scenes: format, camera, subject, clothing, background, lighting, negatives. The script wraps JSON prompts with an instruction to output only the image (without it the model sometimes answers with text).
- **Numbers beat adjectives for framing.** "Close-up" and "tight" are ignored. Use distances ("camera about 35 cm from her face"), lens ("24mm", "12mm fisheye"), and how much of the frame things fill ("head and hat fill about 45 percent of the frame height").
- **References**: say what each reference is for ("image 1 is the pose only, do not copy the person"). Passing every reference to every prompt mixes them: give each generation only the references it needs.
- Describing a pose in words is weak. Pass the pose photo with `--ref`.
- Very long prompts can come back as text. Keep the essentials and put the output instruction first.

## Setup

```bash
pip install gemini-webapi browser-cookie3
```

Authentication, in order:
1. `GEMINI_1PSID` and `GEMINI_1PSIDTS` environment variables.
2. A cookies file at `~/.nano-banana-pro/cookies.json` (or `$NANO_BANANA_HOME/cookies.json`) with `{"__Secure-1PSID": "...", "__Secure-1PSIDTS": "..."}`.
3. `--browser chrome|edge|firefox|brave` (reads the browser session; on Windows this can fail without admin rights, and you must not run it elevated).

Never print, read aloud, log or commit cookie values, and do not open the cookies file just to look at it. The script locks `~/.nano-banana-pro` to the current user on the first run. Windows may save the file as `cookies.json.txt`.

## Limits and care

- This uses the **unofficial** web interface, not the API. It can break when Google changes something, and it spends the plan's daily credits. Do not run it in loops or large batches.
- Expired cookies give exit code 2: ask the user to reopen gemini.google.com while logged in and refresh the cookies.
- Resolution depends on the aspect ratio, not on the model (3:4 and 4:5 come out around 1800x2400 and 1850x2300; 3:2 around 2500x1700).
- For vector logos, simple icons or diagrams, SVG or code is usually better than a generated image.
- Videos are short clips only (a few seconds). Longer or edited video needs another tool.
