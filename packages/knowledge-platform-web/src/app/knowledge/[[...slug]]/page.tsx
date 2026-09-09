import KnowledgeWorkspace, { type WorkspaceSection } from "@/components/KnowledgeWorkspace";

const sections = new Set<WorkspaceSection>(["overview", "library", "search", "sources", "schema", "imports"]);

export default function KnowledgePage({ params }: { params: { slug?: string[] } }) {
  const requested = params.slug?.[0] || "overview";
  const section = sections.has(requested as WorkspaceSection) ? (requested as WorkspaceSection) : "overview";
  return <KnowledgeWorkspace section={section} />;
}
