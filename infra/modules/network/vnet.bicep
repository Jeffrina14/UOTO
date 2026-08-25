param vnetName string
param location string
param aiSubnetName string = 'aiSubnet'
param appIntSubnetName string = 'appIntSubnet'
param gatewaySubnetName string = 'gatewaySubnet'
param appServicesSubnetName string = 'appServicesSubnet'
param databaseSubnetName string = 'databaseSubnet'
param bastionSubnetName string = 'AzureBastionSubnet'

param deployVPN bool = false

param vnetAddress string = '10.0.0.0/23'
// param vnetAddress2 string = '10.0.2.0/23'
param aiSubnetPrefix string = '10.0.0.0/26'
param appIntSubnetPrefix string = '10.0.0.64/26'
param appServicesSubnetPrefix string = '10.0.0.128/26'
param databaseSubnetPrefix string = '10.0.1.0/26'
param gatewaySubnetPrefix string = '10.0.1.64/26'
param bastionSubnetPrefix string = '10.0.1.128/26'

param appServicePlanId string
param appServicePlanName string
param functionAppHostPlan string = 'FlexConsumption'
param tags object = {}
param vnetReuse bool
param existingVnetResourceGroupName string

// Parameters for NSG names
param aiNsgName string = 'ai-nsg'
param appIntNsgName string = 'appInt-nsg'
param appServicesNsgName string = 'appServices-nsg'
param databaseNsgName string = 'database-nsg'
param bastionNsgName string = 'bastion-nsg'

// Network Security Groups
resource aiNsg 'Microsoft.Network/networkSecurityGroups@2024-05-01' = if (!vnetReuse) {
  name: aiNsgName
  location: location
  tags: tags
  properties: {
    securityRules: []
  }
}

resource appIntNsg 'Microsoft.Network/networkSecurityGroups@2024-05-01' = if (!vnetReuse) {
  name: appIntNsgName
  location: location
  tags: tags
  properties: {
    securityRules: []
  }
}

resource appServicesNsg 'Microsoft.Network/networkSecurityGroups@2024-05-01' = if (!vnetReuse) {
  name: appServicesNsgName
  location: location
  tags: tags
  properties: {
    securityRules: []
  }
}

resource databaseNsg 'Microsoft.Network/networkSecurityGroups@2024-05-01' = if (!vnetReuse) {
  name: databaseNsgName
  location: location
  tags: tags
  properties: {
    securityRules: []
  }
}

resource bastionNsg 'Microsoft.Network/networkSecurityGroups@2024-05-01' = if (!vnetReuse) {
  name: bastionNsgName
  location: location
  tags: tags
  properties: {
    securityRules: [
      {
        name: 'AllowHttpsInbound'
        properties: {
          priority: 100
          protocol: 'Tcp'
          access: 'Allow'
          direction: 'Inbound'
          sourceAddressPrefix: 'Internet'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '443'
        }
      }
      {
        name: 'AllowGatewayManagerInbound'
        properties: {
          priority: 120
          protocol: 'Tcp'
          access: 'Allow'
          direction: 'Inbound'
          sourceAddressPrefix: 'GatewayManager'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '443'
        }
      }
      {
        name: 'AllowLoadBalancerInbound'
        properties: {
          priority: 110
          protocol: 'Tcp'
          access: 'Allow'
          direction: 'Inbound'
          sourceAddressPrefix: 'AzureLoadBalancer'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '443'
        }
      }
      {
        name: 'AllowBastionHostCommunicationInBound'
        properties: {
          protocol: '*'
          sourcePortRange: '*'
          sourceAddressPrefix: 'VirtualNetwork'
          destinationPortRanges: [
            '8080'
            '5701'
          ]
          destinationAddressPrefix: 'VirtualNetwork'
          access: 'Allow'
          priority: 130
          direction: 'Inbound'
        }
      }
      {
        name: 'AllowSshRdpOutBound'
        properties: {
          protocol: 'Tcp'
          sourcePortRange: '*'
          sourceAddressPrefix: '*'
          destinationPortRanges: [
            '22'
            '3389'
          ]
          destinationAddressPrefix: 'VirtualNetwork'
          access: 'Allow'
          priority: 100
          direction: 'Outbound'
        }
      }
      {
        name: 'AllowAzureCloudCommunicationOutBound'
        properties: {
          protocol: 'Tcp'
          sourcePortRange: '*'
          sourceAddressPrefix: '*'
          destinationPortRange: '443'
          destinationAddressPrefix: 'AzureCloud'
          access: 'Allow'
          priority: 110
          direction: 'Outbound'
        }
      }
      {
        name: 'AllowBastionHostCommunicationOutBound'
        properties: {
          protocol: '*'
          sourcePortRange: '*'
          sourceAddressPrefix: 'VirtualNetwork'
          destinationPortRanges: [
            '8080'
            '5701'
          ]
          destinationAddressPrefix: 'VirtualNetwork'
          access: 'Allow'
          priority: 120
          direction: 'Outbound'
        }
      }
      {
        name: 'AllowGetSessionInformationOutBound'
        properties: {
          protocol: '*'
          sourcePortRange: '*'
          sourceAddressPrefix: '*'
          destinationAddressPrefix: 'Internet'
          destinationPortRanges: [
            '80'
            '443'
          ]
          access: 'Allow'
          priority: 130
          direction: 'Outbound'
        }
      }
    ]
  }
}

var subnets = [
  {
    name: aiSubnetName
    properties: {
      addressPrefix: aiSubnetPrefix
      privateEndpointNetworkPolicies: 'Enabled'
      privateLinkServiceNetworkPolicies: 'Enabled'
      networkSecurityGroup: {
        id: aiNsg.id
      }
      serviceEndpoints: [
        {
          service: 'Microsoft.Storage'
          locations: [
            location
          ]
        }
      ]
    }
  }
  {
    name: appServicesSubnetName
    properties: {
      addressPrefix: appServicesSubnetPrefix
      privateEndpointNetworkPolicies: 'Enabled'
      privateLinkServiceNetworkPolicies: 'Enabled'
      networkSecurityGroup: {
        id: appServicesNsg.id
      }
      delegations: [
        {
          name: 'appServicesDelegation'
          properties: {
            serviceName: functionAppHostPlan == 'FlexConsumption' ? 'Microsoft.App/environments' : 'Microsoft.Web/serverFarms'
            actions: [
              'Microsoft.Network/virtualNetworks/subnets/action'
            ]
          }
          type: 'Microsoft.Network/virtualNetworks/subnets/delegations'
        }
      ]
    }
  }
  {
    name: databaseSubnetName
    properties: {
      addressPrefix: databaseSubnetPrefix
      privateEndpointNetworkPolicies: 'Enabled'
      privateLinkServiceNetworkPolicies: 'Enabled'
      networkSecurityGroup: {
        id: databaseNsg.id
      }
    }
  }
  {
    name: bastionSubnetName 
    properties: {
      addressPrefix: bastionSubnetPrefix
      privateEndpointNetworkPolicies: 'Enabled'
      privateLinkServiceNetworkPolicies: 'Enabled'
      networkSecurityGroup: {
        id: bastionNsg.id
      }
    }
  }
  {
    name: appIntSubnetName
    properties: {
      addressPrefix: appIntSubnetPrefix
      privateEndpointNetworkPolicies: 'Enabled'
      privateLinkServiceNetworkPolicies: 'Enabled'
      delegations: [
        {
          id: appServicePlanId
          name: appServicePlanName
          properties: {
            serviceName: 'Microsoft.Web/serverFarms'
          }
        }
      ]
      networkSecurityGroup: {
        id: appIntNsg.id
      }
    }
  }
]

var allSubnets = (deployVPN) ? concat(subnets, [{
  name: gatewaySubnetName
  properties: {
    addressPrefix: gatewaySubnetPrefix
    privateEndpointNetworkPolicies: 'Enabled'
    privateLinkServiceNetworkPolicies: 'Enabled'
    delegations: [
    ]
  }
}]) : subnets

resource newVnet 'Microsoft.Network/virtualNetworks@2024-05-01' = if (!vnetReuse) {
  name: vnetName
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: [
        vnetAddress
      ]
    }
    subnets: allSubnets
  }
}

output name string = vnetName
output id string = vnetReuse ? existingVnetId : newVnet.id
output subnets array = vnetReuse ? [] : newVnet.properties.subnets
var existingVnetId = '/subscriptions/${subscription().subscriptionId}/resourceGroups/${existingVnetResourceGroupName}/providers/Microsoft.Network/virtualNetworks/${vnetName}'
output aiSubId string = vnetReuse ? '${existingVnetId}/subnets/${aiSubnetName}' : newVnet.properties.subnets[0].id
output appServicesSubId string = vnetReuse ? '${existingVnetId}/subnets/${appServicesSubnetName}' : newVnet.properties.subnets[1].id
output databaseSubId string = vnetReuse ? '${existingVnetId}/subnets/${databaseSubnetName}' : newVnet.properties.subnets[2].id
output bastionSubId string = vnetReuse ? '${existingVnetId}/subnets/${bastionSubnetName}' : newVnet.properties.subnets[3].id
output appIntSubId string = vnetReuse ? '${existingVnetId}/subnets/${appIntSubnetName}' : newVnet.properties.subnets[4].id
