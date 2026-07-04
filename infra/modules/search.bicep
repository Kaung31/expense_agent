// Azure AI Search — the expense-policy RAG index (Phase 2). Basic tier for reliable
// testing; delete it when idle (it bills hourly). Set sku='free' for $0 if you prefer.

param name string
param location string
param tags object
param principalId string

@allowed(['free', 'basic', 'standard'])
param sku string = 'basic'

// Search Index Data Contributor + Search Service Contributor
var indexDataContributorRoleId = '8ebe5a00-799e-43f5-93ac-243d3dce84a7'
var serviceContributorRoleId = '7ca78c08-252a-4471-8644-bb5ff32d4ba0'

resource search 'Microsoft.Search/searchServices@2024-06-01-preview' = {
  name: name
  location: location
  tags: tags
  sku: { name: sku }
  properties: {
    replicaCount: 1
    partitionCount: 1
    authOptions: null
    disableLocalAuth: true
    semanticSearch: 'free'
  }
}

resource indexDataRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: search
  name: guid(search.id, principalId, indexDataContributorRoleId)
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', indexDataContributorRoleId)
  }
}

resource serviceRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: search
  name: guid(search.id, principalId, serviceContributorRoleId)
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', serviceContributorRoleId)
  }
}

output name string = search.name
output endpoint string = 'https://${search.name}.search.windows.net'
