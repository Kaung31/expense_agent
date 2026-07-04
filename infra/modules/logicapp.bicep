// Logic App (Consumption) — Teams Adaptive Card approval for the HITL step (Phase 3).
//
// Flow (synchronous from the caller's point of view):
//   HTTP POST (expense JSON) ─► post Adaptive Card to a Teams CHANNEL and wait for the
//   human to click Approve/Reject ─► respond to the caller with
//   { decision: "approve"|"reject", approver, respondedAt }.
//
// Because a human click takes longer than the ~2-minute HTTP request window, the Response
// action uses the documented asynchronous-response pattern (operationOptions: Asynchronous):
// the caller gets 202 + a Location header immediately and polls that URL until the click
// produces the final 200 + JSON. The Python ApprovalGateway handles this transparently.
//
// Teams action: `PostCardAndWaitForResponse` — the CURRENT (2026) connector operation.
// (The older "Post an Adaptive Card to a Teams channel and wait for a response" action is
// deprecated per the official connector reference; do not switch back to it.)
//
// ⚠ POST-DEPLOY MANUAL STEP (cannot be done in Bicep): the Teams API connection below is
// created UNAUTHORIZED. In the Azure portal open the resource group → API connection
// "teams-approvals" → Edit API connection → Authorize → sign in with the Microsoft 365
// account that should post the card → Save. Also ensure the Teams admin center allows the
// "Workflows" app (the card is posted by the Flow bot).
//
// Team/channel targeting: `teamsTeamId` (the M365 group id) and `teamsChannelId` are
// workflow parameters. Set the real IDs by editing the defaults below and redeploying, or
// in the portal (Logic app code view → parameters). Find them in Teams: channel → ⋯ →
// "Get link to channel" (the link contains groupId and the channel id).

param namePrefix string
param location string
param tags object
param suffix string

@description('User-assigned identity resource id. Empty → SystemAssigned identity.')
param identityId string

// Unused for Consumption (kept so main.bicep needs no changes).
#disable-next-line no-unused-params
param appInsightsConnectionString string

@description('Teams team id (the underlying M365 group id) to post approval cards into.')
param teamsTeamId string = 'REPLACE_WITH_TEAM_ID'

@description('Teams channel id (e.g. 19:...@thread.tacv2) to post approval cards into.')
param teamsChannelId string = 'REPLACE_WITH_CHANNEL_ID'

var useUserAssigned = !empty(identityId)
var teamsApiId = subscriptionResourceId('Microsoft.Web/locations/managedApis', location, 'teams')

// The Adaptive Card shown to the approver. This is a STRING in the workflow definition;
// the @{...} expressions inside are Logic Apps runtime interpolations, resolved per run.
var approvalCardJson = '''
{
  "type": "AdaptiveCard",
  "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
  "version": "1.4",
  "body": [
    { "type": "TextBlock", "size": "Medium", "weight": "Bolder", "text": "Expense approval needed" },
    { "type": "FactSet", "facts": [
      { "title": "Merchant", "value": "@{triggerBody()?['merchant']}" },
      { "title": "Amount", "value": "@{triggerBody()?['total']} @{triggerBody()?['currency']}" },
      { "title": "Date", "value": "@{triggerBody()?['date']}" },
      { "title": "Category", "value": "@{triggerBody()?['category']}" },
      { "title": "Risk level", "value": "@{triggerBody()?['riskLevel']}" },
      { "title": "Expense ID", "value": "@{triggerBody()?['expenseId']}" }
    ]},
    { "type": "TextBlock", "wrap": true, "text": "Flags: @{join(coalesce(triggerBody()?['riskFlags'], json('[]')), '; ')}" }
  ],
  "actions": [
    { "type": "Action.Submit", "title": "Approve", "id": "Approve" },
    { "type": "Action.Submit", "title": "Reject", "id": "Reject" }
  ]
}
'''

// Managed API connection to Teams. Needs one-time interactive OAuth in the portal (above).
resource teamsConnection 'Microsoft.Web/connections@2016-06-01' = {
  name: 'teams-approvals'
  location: location
  tags: tags
  properties: {
    displayName: 'Teams connection for expense approvals'
    api: {
      id: teamsApiId
    }
  }
}

resource workflow 'Microsoft.Logic/workflows@2019-05-01' = {
  name: 'logic-${namePrefix}-${suffix}'
  location: location
  tags: tags
  identity: useUserAssigned ? {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identityId}': {}
    }
  } : {
    type: 'SystemAssigned'
  }
  properties: {
    state: 'Enabled'
    definition: {
      '$schema': 'https://schema.management.azure.com/providers/Microsoft.Logic/schemas/2016-06-01/workflowdefinition.json#'
      contentVersion: '1.0.0.0'
      parameters: {
        '$connections': {
          type: 'Object'
          defaultValue: {}
        }
        teamsTeamId: {
          type: 'String'
          defaultValue: teamsTeamId
        }
        teamsChannelId: {
          type: 'String'
          defaultValue: teamsChannelId
        }
      }
      triggers: {
        manual: {
          type: 'Request'
          kind: 'Http'
          inputs: {
            schema: {
              type: 'object'
              properties: {
                expenseId: { type: 'string' }
                merchant: { type: 'string' }
                total: { type: 'number' }
                currency: { type: 'string' }
                date: { type: 'string' }
                category: { type: 'string' }
                riskLevel: { type: 'string' }
                riskFlags: { type: 'array', items: { type: 'string' } }
                // Async callback mode (deployed web app): when callbackUrl is present the
                // workflow POSTs the decision back to it after the Teams click.
                callbackUrl: { type: 'string' }
                callbackToken: { type: 'string' }
              }
            }
          }
        }
      }
      actions: {
        // Current Teams operation `PostCardAndWaitForResponse`: posts the card to the
        // channel as the Flow bot and parks the run (webhook continuation) until a button
        // is clicked. Output body carries `submitActionId` + `responder`.
        Post_adaptive_card_and_wait_for_a_response: {
          type: 'ApiConnectionWebhook'
          runAfter: {}
          inputs: {
            host: {
              connection: {
                name: '@parameters(\'$connections\')[\'teams\'][\'connectionId\']'
              }
            }
            body: {
              notificationUrl: '@{listCallbackUrl()}'
              body: {
                messageBody: approvalCardJson
                updateMessage: 'Response recorded — thanks!'
                recipient: {
                  groupId: '@parameters(\'teamsTeamId\')'
                  channelId: '@parameters(\'teamsChannelId\')'
                }
              }
            }
            path: '/v1.0/teams/conversation/gatherinput/poster/@{encodeURIComponent(\'Flow bot\')}/location/@{encodeURIComponent(\'Channel\')}/$subscriptions'
          }
        }
        // Async callback (Stage 2, web app in Azure): if the caller supplied a
        // callbackUrl, push the decision to it. Fire-and-forget callers (the deployed
        // app) rely on this instead of polling the async response below.
        Callback_to_app: {
          type: 'If'
          runAfter: {
            Post_adaptive_card_and_wait_for_a_response: ['Succeeded']
          }
          expression: {
            and: [
              { not: { equals: ['@coalesce(triggerBody()?[\'callbackUrl\'], \'\')', ''] } }
            ]
          }
          actions: {
            POST_decision_to_app: {
              type: 'Http'
              runAfter: {}
              inputs: {
                method: 'POST'
                uri: '@triggerBody()?[\'callbackUrl\']'
                headers: {
                  'Content-Type': 'application/json'
                  'X-Callback-Token': '@{coalesce(triggerBody()?[\'callbackToken\'], \'\')}'
                }
                body: {
                  decision: '@{if(equals(coalesce(body(\'Post_adaptive_card_and_wait_for_a_response\')?[\'submitActionId\'], \'\'), \'Approve\'), \'approve\', \'reject\')}'
                  approver: '@{coalesce(body(\'Post_adaptive_card_and_wait_for_a_response\')?[\'responder\']?[\'email\'], body(\'Post_adaptive_card_and_wait_for_a_response\')?[\'responder\']?[\'displayName\'], \'unknown\')}'
                  respondedAt: '@{utcNow()}'
                }
                retryPolicy: {
                  type: 'exponential'
                  count: 4
                  interval: 'PT10S'   // retries also give a scaled-to-zero app time to wake
                }
              }
            }
          }
        }
        // Asynchronous response pattern: the original caller got 202 + Location up front
        // and polls; once the click lands, the poll returns this 200 + decision JSON.
        Respond_to_caller: {
          type: 'Response'
          kind: 'Http'
          runAfter: {
            Post_adaptive_card_and_wait_for_a_response: ['Succeeded']
          }
          operationOptions: 'Asynchronous'
          inputs: {
            statusCode: 200
            headers: {
              'Content-Type': 'application/json'
            }
            body: {
              decision: '@{if(equals(coalesce(body(\'Post_adaptive_card_and_wait_for_a_response\')?[\'submitActionId\'], \'\'), \'Approve\'), \'approve\', \'reject\')}'
              approver: '@{coalesce(body(\'Post_adaptive_card_and_wait_for_a_response\')?[\'responder\']?[\'email\'], body(\'Post_adaptive_card_and_wait_for_a_response\')?[\'responder\']?[\'displayName\'], \'unknown\')}'
              respondedAt: '@{utcNow()}'
            }
          }
        }
      }
      outputs: {}
    }
    parameters: {
      '$connections': {
        value: {
          teams: {
            connectionId: teamsConnection.id
            connectionName: teamsConnection.name
            id: teamsApiId
          }
        }
      }
    }
  }
}

output name string = workflow.name
// The callback URL the ApprovalGateway POSTs to → goes in .env as APPROVAL_LOGIC_APP_URL.
#disable-next-line outputs-should-not-contain-secrets
output triggerUrl string = listCallbackUrl('${workflow.id}/triggers/manual', workflow.apiVersion).value
