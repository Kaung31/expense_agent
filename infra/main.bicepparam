using 'main.bicep'

param namePrefix = 'expidp'
param environment = 'dev'
param location = 'eastus'

// The guide's models are REAL and GenerallyAvailable in this subscription (verified
// via `az cognitiveservices model list`). gpt-5.4-mini = vision extractor, gpt-5.4 = escalation.
param modelName = 'gpt-5.4-mini'
param escalationModelName = 'gpt-5.4'
param modelVersion = '2026-03-17'
param escalationModelVersion = '2026-03-05'

// Tier 1 (default): only identity + monitoring + Foundry + models.
// Flip individual toggles for Tier 2 (each resource is independent now).
param deployModels = true
param deployStorage = false
param deployCosmos = false
param deploySearch = false
param deployLogicApp = false
