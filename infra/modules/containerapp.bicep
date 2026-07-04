// Azure Container Apps — hosts the FastAPI web app + pipeline (Stage 2).
//
// Consumption plan, `minReplicas: 0` → scales to ZERO when idle (~$0; you pay only
// while a request is being served). HTTP ingress wakes it automatically, which is
// also what makes the async approval callback work after an idle period.
//
// Auth model matches the rest of the repo: the shared user-assigned managed identity
// (AZURE_CLIENT_ID env) — it already holds the Foundry/Cosmos/Search/Storage data-plane
// roles granted by the other modules. AcrPull on the registry is added here. No keys.
//
// First deploy uses a public placeholder image (the app image doesn't exist until you
// run `az acr build`); afterwards: az containerapp update --image <acr>/expense-idp:v1

param namePrefix string
param location string
param tags object
param suffix string

@description('Shared user-assigned managed identity (resource id).')
param identityId string

@description('Client id of that identity — DefaultAzureCredential needs it via AZURE_CLIENT_ID.')
param identityClientId string

@description('Principal id of that identity — for the AcrPull role assignment.')
param identityPrincipalId string

@description('Log Analytics workspace NAME (same resource group) for container logs.')
param workspaceName string

@description('Container image. Placeholder until you `az acr build` the real one.')
param containerImage string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

// Pipeline configuration passed through as env vars (empty = local fallback).
param foundryProjectEndpoint string
param foundryModel string
param foundryEscalationModel string
param cosmosEndpoint string = ''
param searchEndpoint string = ''
param approvalLogicAppUrl string = ''
@secure()
param approvalCallbackToken string = ''
param appInsightsConnectionString string = ''

var appName = 'ca-${namePrefix}-web'

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: workspaceName
}

// ── Container registry (Basic ≈ $5/mo while it exists — see teardown notes) ──
resource acr 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  // 'acr' + prefix(>=3) + suffix(13) is always >= the 5-char minimum; BCP334 can't infer that.
  #disable-next-line BCP334
  name: toLower('acr${namePrefix}${suffix}')
  location: location
  tags: tags
  sku: { name: 'Basic' }
  properties: {
    adminUserEnabled: false   // pulls use the managed identity (AcrPull), not admin keys
  }
}

// AcrPull for the shared identity so the Container App can pull the image keylessly.
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'
resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: acr
  name: guid(acr.id, identityPrincipalId, acrPullRoleId)
  properties: {
    principalId: identityPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
  }
}

// ── Container Apps environment (Consumption — serverless) ────────────────────
resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: 'cae-${namePrefix}'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: workspace.properties.customerId
        sharedKey: workspace.listKeys().primarySharedKey
      }
    }
    workloadProfiles: [
      { name: 'Consumption', workloadProfileType: 'Consumption' }
    ]
  }
}

// ── The app ──────────────────────────────────────────────────────────────────
resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: appName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identityId}': {} }
  }
  properties: {
    environmentId: env.id
    workloadProfileName: 'Consumption'
    configuration: {
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
      }
      registries: [
        {
          server: acr.properties.loginServer
          identity: identityId
        }
      ]
      secrets: empty(approvalCallbackToken) ? [] : [
        { name: 'callback-token', value: approvalCallbackToken }
      ]
    }
    template: {
      containers: [
        {
          name: 'web'
          image: containerImage
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: concat(
            [
              { name: 'AZURE_CLIENT_ID', value: identityClientId }
              { name: 'EXPENSE_MODEL_BACKEND', value: 'foundry' }
              { name: 'FOUNDRY_PROJECT_ENDPOINT', value: foundryProjectEndpoint }
              { name: 'FOUNDRY_MODEL', value: foundryModel }
              { name: 'FOUNDRY_MODEL_ESCALATION', value: foundryEscalationModel }
              { name: 'COSMOS_ENDPOINT', value: cosmosEndpoint }
              { name: 'AZURE_SEARCH_ENDPOINT', value: searchEndpoint }
              { name: 'APPROVAL_LOGIC_APP_URL', value: approvalLogicAppUrl }
              { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsightsConnectionString }
              // The app's own URL — used to build the Logic App callback address.
              { name: 'PUBLIC_BASE_URL', value: 'https://${appName}.${env.properties.defaultDomain}' }
            ],
            empty(approvalCallbackToken) ? [] : [
              { name: 'APPROVAL_CALLBACK_TOKEN', secretRef: 'callback-token' }
            ]
          )
        }
      ]
      scale: {
        minReplicas: 0      // ← scale-to-zero: $0 while idle
        maxReplicas: 1      // single replica keeps the in-memory HITL resume simple
      }
    }
  }
  dependsOn: [ acrPull ]
}

output webAppUrl string = 'https://${app.properties.configuration.ingress.fqdn}'
output acrLoginServer string = acr.properties.loginServer
output acrName string = acr.name
output containerAppName string = app.name
