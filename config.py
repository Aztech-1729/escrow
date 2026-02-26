BOT_TOKEN        = "8617390883:AAGY1kg-YLKc1gwkvWwzg2dK-6MWA1eBawg"
ALLOWED_GROUP_ID = -1003877394701
ADMIN_IDS        = [8313065945, 6670166083]
MONGO_URI        = "mongodb+srv://aztech:ayazahmed1122@cluster0.mhuaw3q.mongodb.net/escrow_db?retryWrites=true&w=majority"
DB_NAME          = "escrow_db"

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
