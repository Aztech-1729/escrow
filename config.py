BOT_TOKEN        = "8617390883:AAGY1kg-YLKc1gwkvWwzg2dK-6MWA1eBawg"

# Escrow group — where escrow deals are made (commands work here)
ESCROW_GROUP_ID  = -1003877394701

# Main chat/dealing group — bot responds to "escrow" keyword here
MAIN_GROUP_ID    = -1003777156200

# Main channel
MAIN_CHANNEL_ID  = -1003621021609

# Keep backward compat (middleware uses this)
ALLOWED_GROUP_ID = ESCROW_GROUP_ID

ADMIN_IDS        = [8313065945, 6670166083]
MONGO_URI        = "mongodb+srv://aztech:ayazahmed1122@cluster0.mhuaw3q.mongodb.net/escrow_db?retryWrites=true&w=majority"
DB_NAME          = "escrow_db"

# Invite links for inline buttons
ESCROW_GROUP_LINK  = "https://t.me/+i01DeGeSiQo4NjQ0"
MAIN_GROUP_LINK    = "https://t.me/+C1gF-nMwvrw5ZGNk"
MAIN_CHANNEL_LINK  = "https://t.me/aurexia_store"

# Greet messages (use {mention} as placeholder for the user's name)
GREET_MAIN_GROUP = (
    "👋 Welcome {mention}!\n\n"
    "This is our main group. For escrow deals, join our Escrow Group using the button below."
)

GREET_ESCROW_GROUP = (
    "👋 Welcome {mention}!\n\n"
    "📖 <b>Escrow Bot — Commands</b>\n\n"
    "<b>For Everyone:</b>\n"
    "• <code>form</code> — Get the escrow deal form template\n"
    "• <code>fee</code> / <code>fees</code> — View the escrow fee structure"
)

FORM_TEMPLATE = """📋 *Escrow Deal Form*

Please fill in all fields and send this message back:

Seller: @username
Buyer: @username
Details: describe the deal
Amount: 0.00
Escrow Till: date or condition
Seller UPI: upi@handle"""

CHARGES_TEXT = (
    "💰 <b>Escrow Fee Structure</b>\n\n"
    "• Under ₹190 → <b>₹10</b>\n"
    "• ₹190 to ₹599 → <b>₹20</b>\n"
    "• ₹600 to ₹2000 → <b>3.5%</b>\n"
    "• ₹2001 to ₹3000 → <b>3%</b>\n"
    "• Above ₹3000 → <b>3%</b>"
)
