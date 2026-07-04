// Cosmos DB (SQL API, serverless) for expense records + duplicate lookup.
// Data-plane access is granted via a Cosmos SQL role assignment (no keys).

param name string
param location string
param tags object
param principalId string

resource account 'Microsoft.DocumentDB/databaseAccounts@2024-11-15' = {
  name: name
  location: location
  tags: tags
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    consistencyPolicy: { defaultConsistencyLevel: 'Session' }
    locations: [ { locationName: location, failoverPriority: 0, isZoneRedundant: false } ]
    capabilities: [ { name: 'EnableServerless' } ]
    disableLocalAuth: true
  }
}

resource database 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-11-15' = {
  parent: account
  name: 'expenses'
  properties: { resource: { id: 'expenses' } }
}

resource container 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-11-15' = {
  parent: database
  name: 'records'
  properties: {
    resource: {
      id: 'records'
      partitionKey: { paths: ['/partition_key'], kind: 'Hash' }
    }
  }
}

// Built-in Cosmos DB Data Contributor (data plane) role.
resource dataContributor 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-11-15' = {
  parent: account
  name: guid(account.id, principalId, 'data-contributor')
  properties: {
    principalId: principalId
    roleDefinitionId: '${account.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002'
    scope: account.id
  }
}

output name string = account.name
output endpoint string = account.properties.documentEndpoint
