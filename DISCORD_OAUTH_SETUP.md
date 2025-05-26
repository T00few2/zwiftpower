# Discord OAuth Setup Guide

This guide will help you set up Discord OAuth authentication for the DZR admin panel.

## Step 1: Create a Discord Application

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications)
2. Click "New Application"
3. Give it a name like "DZR Admin Panel"
4. Click "Create"

## Step 2: Configure OAuth2

1. In your application, go to the "OAuth2" section in the left sidebar
2. Click on "General" under OAuth2
3. Add your redirect URI:
   - For local development: `http://localhost:8080/auth/discord/callback`
   - For production: `https://yourdomain.com/auth/discord/callback`

## Step 3: Get Your Credentials

1. In the "OAuth2 > General" section, copy:
   - **Client ID** 
   - **Client Secret** (click "Reset Secret" if needed)

## Step 4: Configure Environment Variables

Add these to your `.env` file:

```bash
# Discord OAuth Configuration
DISCORD_CLIENT_ID=your_client_id_here
DISCORD_CLIENT_SECRET=your_client_secret_here
DISCORD_REDIRECT_URI=http://localhost:8080/auth/discord/callback

# Flask Session Secret (generate a random string)
FLASK_SECRET_KEY=your-very-secure-random-secret-key

# Your Discord Server ID (already configured)
DISCORD_GUILD_ID=your_discord_guild_id
```

## Step 5: Set Required Permissions

The OAuth flow will request these permissions:
- `identify` - To get the user's Discord username and ID
- `guilds` - To check which servers the user is in and their permissions

## Step 6: Test the Setup

1. Start your Flask application
2. Go to `http://localhost:8080/login`
3. Click "Login with Discord"
4. Authorize the application
5. You should be redirected back and logged in (if you have admin rights)

## Security Notes

- **Admin Check**: Only users with Administrator permissions in your Discord server can access the admin panel
- **Session Security**: Sessions are secured with Flask's session management
- **Audit Trail**: All admin logins are logged to Firebase for security auditing
- **No Password Storage**: No passwords are stored - authentication is handled entirely by Discord

## Troubleshooting

### "Invalid Redirect URI" Error
- Make sure the redirect URI in Discord matches exactly what's in your `.env` file
- Check for trailing slashes or http vs https mismatches

### "Access Denied" Error
- User doesn't have Administrator permissions in the Discord server
- User is not a member of the Discord server
- Check that `DISCORD_GUILD_ID` is correct

### "Authorization Failed" Error
- Check that `DISCORD_CLIENT_ID` and `DISCORD_CLIENT_SECRET` are correct
- Verify the Discord application is properly configured

## Production Deployment

For production:
1. Update `DISCORD_REDIRECT_URI` to use your production domain
2. Add the production redirect URI to your Discord application
3. Use a secure, random `FLASK_SECRET_KEY`
4. Consider using HTTPS for all OAuth flows 