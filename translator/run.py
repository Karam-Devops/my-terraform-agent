# my-terraform-agent/translator/run.py

import os
from . import config, yaml_engine, aws_engine, tf_validator

def _clean_and_format_hcl(raw_hcl):
    """
    Ensures the final output is clean text, stripping any lingering
    markdown fences the LLM might have stubbornly included.
    """
    if not raw_hcl:
        return ""
    
    lines = raw_hcl.splitlines()
    clean_lines = []
    
    for line in lines:
        stripped = line.strip()
        # Remove ```hcl or ```terraform or just ```
        if stripped.startswith("```"):
            continue
        clean_lines.append(line)
        
    return "\n".join(clean_lines)

def run_translation():
    """
    Main orchestration function for the Multi-Cloud Translation Engine.
    Executes Phase 1 (YAML Extraction), Phase 2 (AWS Generation),
    and Pillar 1 Validation (Terraform Init/Validate).
    """
    print("\n" + "="*70)
    print(f"🚀 STARTING MULTI-CLOUD TRANSLATION: Google Cloud -> AWS")
    print("="*70)

    # --- 1. Get Source File ---
    source_file = input("\nEnter the path to the GCP .tf file you want to translate\n(e.g., google_compute_instance_poc_gce.tf): ").strip()

    if not os.path.isfile(source_file):
        print(f"\n❌ Error: File '{source_file}' not found.")
        print("   Please ensure you provide the correct relative or absolute path.")
        return

    try:
        with open(source_file, 'r', encoding='utf-8') as f:
            source_hcl = f.read()
    except Exception as e:
        print(f"\n❌ Error reading source file: {e}")
        return

    # --- 2. Phase 1: Extract Cloud-Agnostic Blueprint (YAML) ---
    yaml_blueprint = yaml_engine.extract_yaml_blueprint(source_hcl, source_file)
    
    if not yaml_blueprint:
        print("\n❌ Translation aborted during YAML extraction phase.")
        return
        
    print("\n   [DEBUG] Extracted Blueprint Preview:")
    print("   " + "-"*40)
    # Print the first few lines to show it's working
    preview_lines = yaml_blueprint.splitlines()[:10]
    for line in preview_lines:
        print(f"   {line}")
    if len(yaml_blueprint.splitlines()) > 10:
         print("   ... (truncated) ...")
    print("   " + "-"*40)

    # --- 3. Phase 2: Generate AWS HCL & Traceability Matrix ---
    raw_aws_hcl = aws_engine.generate_aws_hcl(yaml_blueprint, source_file)
    
    if not raw_aws_hcl:
        print("\n❌ Translation aborted during AWS generation phase.")
        return

    # Clean up any residual markdown formatting
    final_aws_hcl = _clean_and_format_hcl(raw_aws_hcl)

    # --- 4. Define Output Filename ---
    # E.g., google_compute_instance_poc_gce.tf -> aws_translated_poc_gce.tf
    base_name = os.path.basename(source_file)
    if base_name.startswith("google_"):
        # Try to strip the resource type if it matches standard naming
        parts = base_name.split("_", 2)
        if len(parts) >= 3:
            new_filename = "aws_translated_" + parts[-1]
        else:
            new_filename = "aws_translated_" + base_name.replace("google_", "")
    else:
        new_filename = "aws_translated_" + base_name

    output_path = os.path.join(os.path.dirname(source_file), new_filename)

    # --- 5. Pillar 1 Proof: Syntactic Validation ---
    print("\n--- Running Pillar 1 Validation (Syntactic Proof) ---")
    is_valid, validation_msg = tf_validator.validate_aws_hcl(final_aws_hcl)

    # --- 6. Save the Output ---
    try:
        with open(output_path, "w", encoding='utf-8') as f:
            f.write(final_aws_hcl)
        
        if is_valid:
             print(f"\n🎉 SUCCESS! Validated AWS code saved to: {output_path}")
        else:
             print(f"\n⚠️  WARNING: Translated code saved to: {output_path}")
             print("   The code failed automated syntax validation. It requires manual review.")
             print(f"   Validation Error Details:\n{validation_msg}")
             
    except Exception as e:
        print(f"\n❌ Error writing translated file: {e}")
        return

    print("\n" + "="*70)
    print("TRANSLATION COMPLETE")
    print("Please review the generated file, specifically looking for:")
    print("1. The Traceability Matrix explaining architectural decisions.")
    print("2. Any `# TODO:` comments requiring manual parameter injection (e.g., VPC IDs).")
    print("="*70)

if __name__ == "__main__":
    run_translation()