"""T07 短视频合成：卡片分镜 → 1080×1920 H.264 MP4。

- ``storyboard``：分镜 JSON 解析与校验；
- ``frame``：Pillow 自绘卡片（不依赖 T04 渲染代码）；
- ``compose``：ffmpeg concat 组装与产物探测；
- ``tts``：``edge-tts`` 可选配音，失败自动降级静音；
- ``build``：``build_composer() -> VideoComposer``（契约入口）。
"""
