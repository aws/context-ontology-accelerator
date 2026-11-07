// Global Viewer: read-only access across all namespaces
permit (
  principal,
  action == SCL::Action::"viewNamespace",
  resource
)
when { principal.globalRoles.contains("platform-viewer") };

permit (
  principal,
  action == SCL::Action::"viewSource",
  resource
)
when { principal.globalRoles.contains("platform-viewer") };

permit (
  principal,
  action == SCL::Action::"viewOntology",
  resource
)
when { principal.globalRoles.contains("platform-viewer") };

permit (
  principal,
  action == SCL::Action::"list",
  resource
)
when { principal.globalRoles.contains("platform-viewer") };

permit (
  principal,
  action == SCL::Action::"readMetric",
  resource
)
when { principal.globalRoles.contains("platform-viewer") };

permit (
  principal,
  action == SCL::Action::"searchDocuments",
  resource
)
when { principal.globalRoles.contains("platform-viewer") };

permit (
  principal,
  action == SCL::Action::"query",
  resource
)
when { principal.globalRoles.contains("platform-viewer") };

permit (
  principal,
  action == SCL::Action::"translateQuery",
  resource
)
when { principal.globalRoles.contains("platform-viewer") };

permit (
  principal,
  action == SCL::Action::"traverseGraph",
  resource
)
when { principal.globalRoles.contains("platform-viewer") };
