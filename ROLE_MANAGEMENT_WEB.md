# Role Management Web Interface

This document describes the new web-based role management system that provides a comprehensive interface for managing Discord role panels directly from your web dashboard.

## 🌟 Features

### **Visual Role Management**
- **Panel Overview**: View all role panels in a beautiful card-based layout
- **Real-time Role Information**: See role colors, positions, and member counts
- **Status Indicators**: Identify missing/deleted roles at a glance
- **Channel Mapping**: Clear visual mapping of panels to Discord channels
- **Role Prerequisites**: Visual indicators showing which roles require other roles first

### **Advanced Panel Management**
- **Create Panels**: Easy-to-use form for creating new role panels
- **Edit Panels**: Modify panel settings, descriptions, and requirements
- **Delete Panels**: Safe deletion with confirmation prompts
- **Progressive Access**: Set role requirements for accessing specific panels
- **Individual Role Prerequisites**: Set specific role requirements for each role within panels

### **Comprehensive Statistics**
- **Total Panels**: Number of active role panels
- **Total Roles**: Count of all self-assignable roles
- **Active Channels**: Number of channels with role panels

## 🚀 Getting Started

### Access the Interface
1. Log in to your admin dashboard
2. Click on the **"🎭 Role Management"** card
3. You'll be taken to `/roles` - the main role management interface

### Dashboard Integration
The role management system is fully integrated into your existing dashboard:
- Uses the same authentication system
- Matches the design language
- Includes breadcrumb navigation
- Provides consistent user experience

## 🛠️ API Endpoints

### **Panel Management**

#### Get All Panels
```http
GET /api/roles/panels
```
Returns all role panels with complete role information and Discord data.

#### Create Panel
```http
POST /api/roles/panels
```
**Body:**
```json
{
  "panelId": "basic-roles",
  "name": "Basic Roles",
  "description": "Select your basic server roles",
  "channelId": "1234567890123456789",
  "requiredRoles": ["987654321098765432"],
  "approvalChannelId": "1234567890123456789"
}
```

#### Update Panel
```http
PUT /api/roles/panels/{panelId}
```

#### Delete Panel
```http
DELETE /api/roles/panels/{panelId}
```

### **Role Management**

#### Add Role to Panel
```http
POST /api/roles/panels/{panelId}/roles
```
**Body:**
```json
{
  "roleId": "1234567890123456789",
  "roleName": "Member",
  "description": "Basic member role",
  "emoji": "👤",
  "requiresApproval": false,
  "requiredRoles": ["987654321098765432"]
}
```

#### Remove Role from Panel
```http
DELETE /api/roles/panels/{panelId}/roles/{roleId}
```

#### Update Role Prerequisites
```http
PUT /api/roles/panels/{panelId}/roles/{roleId}/prerequisites
```
**Body:**
```json
{
  "requiredRoles": ["987654321098765432", "123456789012345678"]
}
```

### **Discord Integration**

#### Get Guild Roles
```http
GET /api/roles/guild-roles
```
Returns all available Discord roles (excluding managed roles and @everyone).

#### Get Discord Channels
```http
GET /api/discord/channels
```
Returns all text channels in the Discord server.

## 🔧 Technical Architecture

### **Backend Integration**
- **Firebase Integration**: Uses the same Firebase database as your Discord bot
- **Discord API**: Real-time integration with Discord for role and channel data
- **Authentication**: Leverages existing Discord OAuth with admin validation
- **Error Handling**: Comprehensive error handling with user-friendly messages

### **Frontend Features**
- **Responsive Design**: Works on desktop, tablet, and mobile devices
- **Real-time Updates**: Automatic refresh of role information
- **Loading States**: Smooth loading animations and feedback
- **Form Validation**: Client-side and server-side validation
- **Modal Dialogs**: Clean, accessible modal interfaces

### **Data Structure**
The web interface works with the same data structure as your Discord bot:

```javascript
{
  "panels": {
    "basic": {
      "channelId": "1234567890123456789",
      "name": "Basic Roles",
      "description": "Click buttons to add/remove roles",
      "roles": [
        {
          "roleId": "1234567890123456789",
          "roleName": "Member",
          "description": "Basic member role",
          "emoji": "👤",
          "requiresApproval": false,
          "addedAt": "2024-01-01T00:00:00Z"
        }
      ],
      "requiredRoles": [],
      "approvalChannelId": null,
      "order": 1,
      "createdAt": "2024-01-01T00:00:00Z",
      "updatedAt": "2024-01-01T00:00:00Z"
    }
  }
}
```

## 🎨 User Interface

### **Panel Cards**
Each role panel is displayed as a card containing:
- **Header**: Panel name with edit/delete actions
- **Description**: Panel description text
- **Channel**: Visual channel badge showing where the panel is located
- **Role Count**: Number of roles in the panel
- **Role List**: Color-coded role badges with emojis
- **Add Role Button**: Quick access to add more roles

### **Create Panel Modal**
A comprehensive form with:
- **Panel ID**: Auto-sanitized unique identifier
- **Panel Name**: Display name for the panel
- **Description**: Custom description (with default)
- **Channel Selection**: Dropdown of available Discord channels
- **Required Roles**: Multi-select for progressive access
- **Approval Channel**: Optional approval workflow

### **Statistics Overview**
Dashboard-style statistics cards showing:
- Total number of panels
- Total number of assignable roles
- Number of active channels

## 🔒 Security & Permissions

### **Authentication**
- **Discord OAuth**: Users must authenticate with Discord
- **Admin Validation**: Only users with admin permissions in your Discord server can access
- **Session Management**: Secure session handling with timeout

### **Discord Permissions**
The system validates that your bot has the necessary permissions:
- **Manage Roles**: Required for role assignment
- **Send Messages**: Needed for panel creation
- **Embed Links**: Required for rich embeds
- **View Channel**: Must be able to see target channels

### **Data Validation**
- **Panel ID Sanitization**: Ensures safe, URL-friendly identifiers
- **Role Hierarchy**: Validates bot can manage selected roles
- **Channel Access**: Verifies bot has permissions in target channels

## 🔑 Role Prerequisites

The web interface now supports setting **individual role prerequisites** - specific roles that users must have before they can obtain other roles within a panel.

### **How It Works**

1. **Panel-Level Requirements**: Control who can access the entire panel
2. **Individual Role Requirements**: Control who can get specific roles within a panel

### **Setting Prerequisites**

#### **When Adding a New Role**
1. Click **"Add Role"** on any panel
2. Select the role from the dropdown
3. Use the **"Required Roles (Prerequisites)"** multi-select field
4. Hold Ctrl/Cmd to select multiple prerequisite roles
5. Complete other settings and click **"Add Role"**

#### **When Editing an Existing Role**
1. Click the **"✏️"** edit button next to any role
2. Use the **"Required Roles (Prerequisites)"** multi-select field
3. Select which roles users must have first
4. Click **"Update Role"** to save changes

### **Visual Indicators**

- **Prerequisites Badge**: Roles with prerequisites show a **"🔑 X Prerequisites"** badge
- **Tooltip Information**: Hover over any role to see detailed prerequisite information
- **Color Coding**: Prerequisites are shown with an orange color scheme

### **Example Use Cases**

- **Gaming Community**: Require "Member" role before getting "Pro Player" role
- **Work Teams**: Require "Employee" role before getting "Team Lead" role
- **Educational**: Require "Student" role before getting "Advanced" role

## 🚀 Future Enhancements

### **Planned Features**
- **Role Analytics**: Usage statistics and popularity metrics
- **Bulk Operations**: Import/export role configurations
- **Role Templates**: Save and reuse common role setups
- **Visual Hierarchy Builder**: Drag-and-drop role requirement setup
- **Approval Queue Management**: Web interface for pending approvals
- **Public Role Browser**: Let users preview roles before joining Discord

### **Advanced Integrations**
- **Webhook Notifications**: Real-time updates to Discord when changes are made
- **Audit Logging**: Track all changes made through the web interface
- **Role Synchronization**: Sync role assignments across multiple servers
- **Custom Themes**: Personalized color schemes and branding

## 🛠️ Development Notes

### **File Structure**
```
zwiftpower/
├── main.py                    # Main Flask application with new role endpoints
├── templates/
│   ├── roles_overview.html    # Main role management interface
│   └── dashboard.html         # Updated with role management link
├── firebase.py                # Updated with set_document function
└── discord_api.py             # Discord API integration
```

### **Dependencies**
The role management system uses existing dependencies:
- **Flask**: Web framework
- **Firebase Admin SDK**: Database operations
- **Requests**: Discord API calls
- **Python-dotenv**: Environment configuration

### **Environment Variables**
Make sure these are configured:
```env
DISCORD_BOT_TOKEN=your_bot_token
DISCORD_GUILD_ID=your_guild_id
DISCORD_CLIENT_ID=your_client_id
DISCORD_CLIENT_SECRET=your_client_secret
```

## 📞 Support

If you encounter any issues with the role management system:

1. **Check Bot Permissions**: Ensure your Discord bot has the required permissions
2. **Verify Environment Variables**: Make sure all Discord configuration is correct
3. **Check Logs**: Look for error messages in the Flask application logs
4. **Test Discord Commands**: Verify the bot's role system works via Discord commands

The web interface is designed to complement, not replace, your existing Discord role commands. Both systems work with the same underlying data structure and can be used interchangeably. 