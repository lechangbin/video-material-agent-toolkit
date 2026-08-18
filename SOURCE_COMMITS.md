# Source commits

Toolkit version `0.2.0` imports source snapshots from these local Git repositories:

| Package | Commit | Subject |
| --- | --- | --- |
| `material-collector` | `7839d3d6c97ad3f903e79cdc0a245b26633aa8fc` | `feat: orchestrate search understanding and refinement` |
| `semvideo` | `3728b51d9e16433d879ccd6e40e007b700a2439b` | `fix: enforce atomic workspace job admission` |

The distribution repository excludes source-repository-only Agent scratch files,
Codex settings and nested Skill copies. Release documentation, MIT metadata and
the top-level multi-Agent Skill layout are maintained in this repository.

The material-collector platform adapters were independently implemented from
public platform response behavior. The source project records that no
MediaCrawler or Douyin_TikTok_Download_API file or function was copied into the
current implementation. Any future copied or derived third-party code must carry
its original copyright, license, fixed source commit and local modification
notice before a release is published.
