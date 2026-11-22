import { notFound } from "next/navigation";
import Planning, { type PlanningProject } from "./planning";

export default async function Analysis({ params }: { params: Promise<{ projectId: string }> }) {
  const { projectId } = await params;
  const backend = process.env.API_ORIGIN ?? "http://127.0.0.1:8000";
  const response = await fetch(backend + "/api/projects/" + encodeURIComponent(projectId) + "/video-analysis", { cache: "no-store" });
  if (response.status === 404) notFound();
  if (!response.ok) throw new Error("프로젝트를 불러오지 못했습니다.");
  const project: PlanningProject = await response.json();
  return <Planning projectId={projectId} project={project} />;
}
