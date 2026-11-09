def combine_proof_verdict(ai_data: dict, expected_code: str) -> dict:
    """Combine the vision model's findings into a fail-closed proof decision."""
    detected_code = "".join(
        character
        for character in str(ai_data.get("detected_code", "")).upper()
        if character.isalnum()
    )
    expected_clean = "".join(
        character for character in str(expected_code).upper() if character.isalnum()
    )
    reasoning = str(ai_data.get("reasoning", "")).strip()
    item_matches = ai_data.get("item_matches") is True
    confidence = str(ai_data.get("confidence", "LOW")).strip().upper()
    tracking = str(ai_data.get("tracking_found", "NONE")).strip() or "NONE"

    raw_condition = str(ai_data.get("product_condition", "UNCLEAR")).strip().upper()
    product_condition = (
        raw_condition
        if raw_condition in {"GOOD", "ISSUES", "UNCLEAR", "NOT_APPLICABLE"}
        else "UNCLEAR"
    )
    raw_issues = ai_data.get("product_issues", [])
    if isinstance(raw_issues, list):
        product_issues = [
            str(issue).strip() for issue in raw_issues if str(issue).strip()
        ]
    else:
        product_issues = []
        product_condition = "UNCLEAR"

    if not expected_clean or expected_clean == "UNKNOWN":
        decision = "ASK_BUYER"
        reason = "No valid expected verification code is available; manual buyer review is required."
    elif detected_code != expected_clean:
        decision = "HOLD"
        reason = (
            f"Verification code mismatch. Expected '{expected_code.strip().upper()}', "
            f"but saw '{detected_code or 'NONE'}'."
        )
    elif product_issues or product_condition == "ISSUES":
        decision = "ASK_BUYER"
        issue_details = "; ".join(product_issues) or reasoning or "The product condition may have an issue."
        reason = f"Potential product defect or mismatch needs buyer review: {issue_details}"
    elif not item_matches:
        decision = "ASK_BUYER"
        reason = (
            "The product does not clearly match the confirmed deal terms and needs buyer review: "
            f"{reasoning or 'The image match was inconclusive.'}"
        )
    elif product_condition != "GOOD" or confidence != "HIGH":
        decision = "ASK_BUYER"
        reason = (
            "The product's condition or match needs buyer review: "
            f"{reasoning or 'The image does not clearly establish the product condition.'}"
        )
    else:
        decision = "RELEASE"
        reason = (
            f"Code '{expected_clean}' and product visually verified: "
            f"{reasoning or 'The product matches the deal terms and no visible issues were found.'}"
        )

    return {
        "decision": decision,
        "reason": reason,
        "detected_code": detected_code,
        "tracking": tracking,
        "product_condition": product_condition,
        "product_issues": product_issues,
        "ai_details": reasoning,
    }
