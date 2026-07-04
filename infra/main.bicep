// ── Expense IDP — top-level infrastructure (guide Phase 0) ──────────────────
// Subscription-scoped: creates the resource group, then all resources with a
// shared user-assigned managed identity and RBAC (no API keys — guide §8).
//
//   az deployment sub create \
//     --location eastus \
//     --template-file infra/main.bicep \
//     --parameters infra/main.bicepparam
//
// Toggles let you deploy in tiers:
//   Tier 1 (default): identity + monitoring + Foundry + models  (deployData=false, deployLogicApp=false)
//   Tier 2: add Cosmos + Search + Storage (deployData=true), Logic App (deployLogicApp=true)

targetScope = 'subscription'

@description('Short prefix for resource names (lowercase letters/numbers).')
@minLength(3)
@maxLength(11)
param namePrefix string = 'expidp'

@description('Deployment environment tag.')
@allowed(['dev', 'test', 'prod'])
param environment string = 'dev'

@description('Azure region for all resources.')
param location string = 'eastus'

@description('Vision/extractor model deployment (the guide\'s gpt-5.4-mini, GA in this sub).')
param modelName string = 'gpt-5.4-mini'

@description('Escalation/reasoning model deployment (gpt-5.4).')
param escalationModelName string = 'gpt-5.4'

@description('Model versions — blank lets Azure pick the current default (az cognitiveservices model list).')
param modelVersion string = '2026-03-17'
param escalationModelVersion string = '2026-03-05'

@description('Deploy the two model deployments (set false to just create the Foundry account first).')
param deployModels bool = true

@description('Deploy the receipts Blob Storage account (Tier 2).')
param deployStorage bool = false

@description('Deploy Cosmos DB for records + duplicate lookup (Tier 2).')
param deployCosmos bool = false

@description('Deploy Azure AI Search for policy RAG (Tier 2). Free SKU by default → $0.')
param deploySearch bool = false

@description('Deploy the Consumption Logic App (serverless, no VM quota needed).')
param deployLogicApp bool = false

@description('Deploy the web app on Azure Container Apps (scale-to-zero) + its ACR.')
param deployWebApp bool = false

@description('Web app container image. Placeholder until `az acr build` produces the real one.')
param containerImage string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

@description('Optional shared secret the Logic App echoes back on approval callbacks.')
@secure()
param approvalCallbackToken string = ''

var tags = {
  application: 'expense-idp'
  environment: environment
}
var rgName = 'rg-${namePrefix}-${environment}'
var suffix = uniqueString(subscription().id, rgName)

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: rgName
  location: location
  tags: tags
}

module identity 'modules/identity.bicep' = {
  scope: rg
  name: 'identity'
  params: { namePrefix: namePrefix, location: location, tags: tags }
}

module monitoring 'modules/monitoring.bicep' = {
  scope: rg
  name: 'monitoring'
  params: { namePrefix: namePrefix, location: location, tags: tags }
}

module foundry 'modules/foundry.bicep' = {
  scope: rg
  name: 'foundry'
  params: {
    name: toLower('aif-${namePrefix}-${suffix}')
    location: location
    tags: tags
    principalId: identity.outputs.principalId
    modelName: modelName
    escalationModelName: escalationModelName
    modelVersion: modelVersion
    escalationModelVersion: escalationModelVersion
    deployModels: deployModels
  }
}

module storage 'modules/storage.bicep' = if (deployStorage) {
  scope: rg
  name: 'storage'
  params: {
    name: toLower('st${namePrefix}${suffix}')
    location: location
    tags: tags
    principalId: identity.outputs.principalId
  }
}

module cosmos 'modules/cosmos.bicep' = if (deployCosmos) {
  scope: rg
  name: 'cosmos'
  params: {
    name: toLower('cosmos-${namePrefix}-${suffix}')
    location: location
    tags: tags
    principalId: identity.outputs.principalId
  }
}

module search 'modules/search.bicep' = if (deploySearch) {
  scope: rg
  name: 'search'
  params: {
    name: toLower('srch-${namePrefix}-${suffix}')
    location: location
    tags: tags
    principalId: identity.outputs.principalId
  }
}

module logicApp 'modules/logicapp.bicep' = if (deployLogicApp) {
  scope: rg
  name: 'logicapp'
  params: {
    namePrefix: namePrefix
    location: location
    tags: tags
    suffix: suffix
    identityId: identity.outputs.id
    appInsightsConnectionString: monitoring.outputs.appInsightsConnectionString
  }
}

module webApp 'modules/containerapp.bicep' = if (deployWebApp) {
  scope: rg
  name: 'webapp'
  params: {
    namePrefix: namePrefix
    location: location
    tags: tags
    suffix: suffix
    identityId: identity.outputs.id
    identityClientId: identity.outputs.clientId
    identityPrincipalId: identity.outputs.principalId
    workspaceName: monitoring.outputs.workspaceName
    containerImage: containerImage
    foundryProjectEndpoint: foundry.outputs.projectEndpoint
    foundryModel: modelName
    foundryEscalationModel: escalationModelName
    cosmosEndpoint: deployCosmos ? cosmos!.outputs.endpoint : ''
    searchEndpoint: deploySearch ? search!.outputs.endpoint : ''
    approvalLogicAppUrl: deployLogicApp ? logicApp!.outputs.triggerUrl : ''
    approvalCallbackToken: approvalCallbackToken
    appInsightsConnectionString: monitoring.outputs.appInsightsConnectionString
  }
}

output resourceGroup string = rg.name
output managedIdentityClientId string = identity.outputs.clientId
output foundryEndpoint string = foundry.outputs.endpoint
output foundryProjectEndpoint string = foundry.outputs.projectEndpoint
output cosmosEndpoint string = deployCosmos ? cosmos!.outputs.endpoint : ''
output searchEndpoint string = deploySearch ? search!.outputs.endpoint : ''
output blobEndpoint string = deployStorage ? storage!.outputs.blobEndpoint : ''
output appInsightsConnectionString string = monitoring.outputs.appInsightsConnectionString
output webAppUrl string = deployWebApp ? webApp!.outputs.webAppUrl : ''
output acrLoginServer string = deployWebApp ? webApp!.outputs.acrLoginServer : ''
output acrName string = deployWebApp ? webApp!.outputs.acrName : ''
output containerAppName string = deployWebApp ? webApp!.outputs.containerAppName : ''
