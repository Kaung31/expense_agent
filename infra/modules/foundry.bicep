// Azure AI Foundry (AIServices account + project) with two model deployments:
// the default vision/extractor model and the escalation/reasoning model (guide §3).

param name string
param location string
param tags object
param principalId string
param modelName string
param escalationModelName string
param deployModels bool = true

@description('Model + version pairs. Update versions to what your region offers.')
param modelVersion string = '2025-04-14'
param escalationModelVersion string = '2025-04-14'

// Cognitive Services OpenAI User (data-plane inference)
var openAiUserRoleId = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'

resource account 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' = {
  name: name
  location: location
  tags: tags
  kind: 'AIServices'
  sku: { name: 'S0' }
  identity: { type: 'SystemAssigned' }
  properties: {
    customSubDomainName: name
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: true
    allowProjectManagement: true   // required to create the Foundry (V2) project sub-resource
  }
}

// Foundry project (V2 project surface used by FoundryChatClient / Agent Service).
resource project 'Microsoft.CognitiveServices/accounts/projects@2025-04-01-preview' = {
  parent: account
  name: '${name}-proj'
  location: location
  identity: { type: 'SystemAssigned' }
  properties: {}
}

// Deployments must be sequential (Cognitive Services serialises them).
resource visionDeployment 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = if (deployModels) {
  parent: account
  name: modelName
  sku: { name: 'GlobalStandard', capacity: 30 }
  properties: {
    // Omit version when blank → Azure deploys the current default (non-deprecated) version.
    model: union({ format: 'OpenAI', name: modelName }, empty(modelVersion) ? {} : { version: modelVersion })
  }
}

resource escalationDeployment 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = if (deployModels) {
  parent: account
  name: escalationModelName
  dependsOn: [ visionDeployment ]
  sku: { name: 'GlobalStandard', capacity: 30 }
  properties: {
    model: union({ format: 'OpenAI', name: escalationModelName }, empty(escalationModelVersion) ? {} : { version: escalationModelVersion })
  }
}

resource openAiUserRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: account
  name: guid(account.id, principalId, openAiUserRoleId)
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', openAiUserRoleId)
  }
}

output name string = account.name
output endpoint string = account.properties.endpoint
output projectEndpoint string = 'https://${name}.services.ai.azure.com/api/projects/${project.name}'
