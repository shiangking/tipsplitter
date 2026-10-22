# Vision Capability Matrix (E2-A probe)

Generated: 2026-10-22T14:51:57+00:00

| Profile | code-image round | screenshot-analysis round | Vision-capable |
|---|---|---|---|
| `glm5.3` | ❌ | ❌ | **NO** |
| `deepseekv4.1` | ❌ | ❌ | **NO** |
| `qwen3.8flash` | ❌ | ❌ | **NO** |

Probe method: for each profile two throwaway conversations were launched with an image attachment (PNG data URL). Round 1 requires reading a 6-character code from a rendered image; round 2 requires structural analysis of a rendered mock UI screenshot. Answers were checked against ground truth known only to this script. Probe conversations are deleted after each round; evidence images and raw replies are kept under `state/vision-probe/`.

**Policy:** visual-review gates may only use profiles marked vision-capable here. If none qualify, visual gates report `unavailable` and block unattended product-quality acceptance.
