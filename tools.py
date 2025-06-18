# tools.py

def check_payment_status(DPD):
    if ( DPD==0):
        return "payment completed"
    else:
        return "payment not completed"
    

tools_defined = [
    
    {
        "type": "function",
        "name": "check_payment_status",
        "description": "Get the status of the last loan payment",
        "parameters": {
            "type": "object",
            "properties": {
                "DPD": {"type": "string"}
            },
            "required": [ "DPD"]
        },
    }
]
