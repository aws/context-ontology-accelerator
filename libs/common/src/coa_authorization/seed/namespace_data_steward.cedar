// Namespace DataSteward: manage data assets within their namespace.
// Role name "data-steward" (kebab-case) matches the identifier stored in DynamoDB.
//
// Grants:
//   - manageSource, manageOntology, manageMetric (scoped via resource.namespace)
//   - query, readMetric, searchDocuments (scoped via resource.namespace)
//   - viewNamespace, list (scoped via resource.id)
//
// Does NOT grant: manageNamespace, deleteNamespace, grantPermissions, manageRoles.
permit (
  principal,
  action == SCL::Action::"manageSource",
  resource
)
when {
  principal.resourceRoles.contains({
    "role": "data-steward",
    "resourceUID": resource.namespace
  })
};

permit (
  principal,
  action == SCL::Action::"manageOntology",
  resource
)
when {
  principal.resourceRoles.contains({
    "role": "data-steward",
    "resourceUID": resource.namespace
  })
};

permit (
  principal,
  action == SCL::Action::"manageMetric",
  resource
)
when {
  principal.resourceRoles.contains({
    "role": "data-steward",
    "resourceUID": resource.namespace
  })
};

permit (
  principal,
  action == SCL::Action::"viewNamespace",
  resource
)
when {
  principal.resourceRoles.contains({
    "role": "data-steward",
    "resourceUID": resource.id
  })
};

permit (
  principal,
  action == SCL::Action::"viewSource",
  resource
)
when {
  principal.resourceRoles.contains({
    "role": "data-steward",
    "resourceUID": resource.namespace
  })
};

permit (
  principal,
  action == SCL::Action::"viewOntology",
  resource
)
when {
  principal.resourceRoles.contains({
    "role": "data-steward",
    "resourceUID": resource.namespace
  })
};

permit (
  principal,
  action == SCL::Action::"list",
  resource
)
when {
  principal.resourceRoles.contains({
    "role": "data-steward",
    "resourceUID": resource.id
  })
};

permit (
  principal,
  action == SCL::Action::"query",
  resource
)
when {
  principal.resourceRoles.contains({
    "role": "data-steward",
    "resourceUID": resource.namespace
  })
};

permit (
  principal,
  action == SCL::Action::"readMetric",
  resource
)
when {
  principal.resourceRoles.contains({
    "role": "data-steward",
    "resourceUID": resource.namespace
  })
};

permit (
  principal,
  action == SCL::Action::"searchDocuments",
  resource
)
when {
  principal.resourceRoles.contains({
    "role": "data-steward",
    "resourceUID": resource.namespace
  })
};

permit (
  principal,
  action == SCL::Action::"translateQuery",
  resource
)
when {
  principal.resourceRoles.contains({
    "role": "data-steward",
    "resourceUID": resource.namespace
  })
};

permit (
  principal,
  action == SCL::Action::"traverseGraph",
  resource
)
when {
  principal.resourceRoles.contains({
    "role": "data-steward",
    "resourceUID": resource.namespace
  })
};
