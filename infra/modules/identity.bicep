// Shared user-assigned managed identity — used by the workflow/Logic App to reach
// Foundry, Cosmos, Search, and Storage via RBAC (Entra Agent ID pattern, no keys).

param namePrefix string
param location string
param tags object

resource uami 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-${namePrefix}'
  location: location
  tags: tags
}

output id string = uami.id
output principalId string = uami.properties.principalId
output clientId string = uami.properties.clientId
