import { roleAssignmentInfo } from '../security/managed-identity.bicep'

@description('Role assignments to create for the Resource Group.')
param roleAssignments roleAssignmentInfo[] = []
@description('Role assignment resource IDs to skip because equivalent assignments already exist.')
param skipRoleAssignmentIds array = []

resource assignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for roleAssignment in roleAssignments: if (!contains(skipRoleAssignmentIds, guid(resourceGroup().id, roleAssignment.principalId, roleAssignment.roleDefinitionId))) {
    name: guid(resourceGroup().id, roleAssignment.principalId, roleAssignment.roleDefinitionId)
    scope: resourceGroup()
    properties: {
      principalId: roleAssignment.principalId
      roleDefinitionId: roleAssignment.roleDefinitionId
      principalType: roleAssignment.principalType
    }
  }
]
