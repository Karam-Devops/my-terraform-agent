# my-terraform-agent/translator/run.py

import os
from . import config, yaml_engine, aws_engine, azure_engine, tf_validator

def _clean_and_format_hcl(raw_hcl):
    if not raw_hcl: return ""
    clean_lines = [line for line in raw_hcl.splitlines() if not line.strip().startswith("```")]
    return "\n".join(clean_lines)

def run_translation():
    print("\n" + "="*70)
    print("   MULTI-CLOUD IaC TRANSLATION ENGINE")
    print("="*70)

    # 1. Choose Target Cloud
    while True:
        target = input("\nTranslate GCP resource to [AWS] or [Azure]? (Enter 'aws' or 'azure'): ").strip().lower()
        if target in ['aws', 'azure']:
            break
        print("❌ Invalid choice. Please enter 'aws' or 'azure'.")

    # 2. Get Source File
    source_file = input("\nEnter the path to the GCP .tf file you want to translate: ").strip()
    if not os.path.isfile(source_file):
        print(f"\n❌ Error: File '{source_file}' not found."); return

    try:
        with open(source_file, 'r', encoding='utf-8') as f:
            source_hcl = f.read()
    except Exception as e:
        print(f"\n❌ Error reading source file: {e}"); return

    # 3. Phase 1: Extract Blueprint (Shared logic)
    yaml_blueprint = yaml_engine.extract_yaml_blueprint(source_hcl, source_file)
    if not yaml_blueprint: return
        
    print("\n   [DEBUG] Extracted Blueprint Preview:")
    for line in yaml_blueprint.splitlines()[:5]: print(f"   {line}")
    print("   ...")

    # 4. Phase 2: Generate Target HCL (Routed logic)
    if target == "aws":
        raw_target_hcl = aws_engine.generate_aws_hcl(yaml_blueprint, source_file)
        prefix = "aws"
    else:
        raw_target_hcl = azure_engine.generate_azure_hcl(yaml_blueprint, source_file)
        prefix = "azure"
    
    if not raw_target_hcl: return
    final_target_hcl = _clean_and_format_hcl(raw_target_hcl)

    # 5. Define Output Filename
    base_name = os.path.basename(source_file)
    clean_name = base_name.replace("google_", "")
    new_filename = f"{prefix}_translated_{clean_name}"
    output_path = os.path.join(os.path.dirname(source_file), new_filename)

    # 6. Pillar 1 Proof: Syntactic Validation
    is_valid, validation_msg = tf_validator.validate_hcl(final_target_hcl, target)

    # 7. Save the Output
    try:
        with open(output_path, "w", encoding='utf-8') as f: f.write(final_target_hcl)
        
        if is_valid:
             print(f"\n🎉 SUCCESS! Validated {target.upper()} code saved to: {output_path}")
        else:
             print(f"\n⚠️  WARNING: Translated code saved to: {output_path}")
             print("   The code failed automated syntax validation. It requires manual review.")
             print(f"   Validation Error Details:\n{validation_msg}")
             
    except Exception as e:
        print(f"\n❌ Error writing translated file: {e}"); return

    print("\n" + "="*70)
    print(f"TRANSLATION COMPLETE ({target.upper()})")
    print("="*70)

if __name__ == "__main__":
    run_translation()