// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { createLazyFileRoute, Navigate } from "@tanstack/react-router";

// v3 redirect — the per-stage video page was merged into /beats. Kept so legacy bookmarks keep working.
function VideoRedirect() {
  const { project, episode } = Route.useParams();
  return (
    <Navigate
      to="/projects/$project/episodes/$episode/beats"
      params={{ project, episode }}
      search={{ sub: "video" } as never}
      replace
    />
  );
}

export const Route = createLazyFileRoute(
  "/_app/projects/$project/episodes/$episode/video",
)({
  component: VideoRedirect,
});
