"""Builders for SYNTHETIC .epmp documents in the confirmed export shape.

Field layouts mirror a fresh-set EPM export (Windows policies/groups) and a
macOS policy export. Every value here is made up: names start with "Test",
GUIDs are of the form 00000000-0000-4000-8xxx-..., apps are testappNNNN.
Never put real export data in this repo.
"""

ZERO = "00000000-0000-0000-0000-000000000000"


def guid(kind, n):
    """Obviously fake GUID. kind: 1 = group, 2 = policy, 3 = Windows app, 4 = Mac app."""
    return f"00000000-0000-4000-8{kind:03d}-{n:012d}"


def _ui(dialog_type):
    return {"AllowedDialogType": dialog_type, "Id": ZERO}


def win_app(n):
    """Windows executable definition (applicationType 3)."""
    return {
        "applicationType": 3,
        "childProcess": False,
        "description": "",
        "displayName": "",
        "id": guid(3, n),
        "patterns": {
            "PUBLISHER": {"@type": "Publisher", "caseSensitive": False, "compareAs": 1,
                          "content": "Test Publisher", "isEmpty": False, "separator": ";",
                          "signatureLevel": 2},
            "LOCATION": {"@type": "Location", "caseSensitive": False,
                         "content": "C:\\TestApps", "isEmpty": False, "withSubfolders": True},
            "ORIGINAL_FILE_NAME": {"@type": "FileInfo", "elementName": "FileVerInfo",
                                   "attributeInfoName": "OriginalFilename", "caseSensitive": False,
                                   "compareAs": 0, "content": f"testapp{n:04d}.exe",
                                   "isEmpty": False},
        },
        "protectInstalledFiles": False,
        "restrictOpenSaveFileDialog": True,
    }


def mac_app(n):
    """macOS file-name definition (applicationType 24)."""
    return {
        "applicationType": 24,
        "childProcess": False,
        "description": "",
        "displayName": "",
        "id": guid(4, n),
        "patterns": {
            "FILE_NAME": {"@type": "FileName", "caseSensitive": False, "compareAs": 1,
                          "content": f"testmacapp{n:04d}", "fileSize": 0, "hash": "",
                          "hashAlgorithm": "", "hashSHA256": "", "isEmpty": False},
        },
        "protectInstalledFiles": False,
        "restrictOpenSaveFileDialog": False,
    }


def group(n, name, apps, policy_type=14):
    """AppGroup (PolicyType 14 Windows, 16 macOS)."""
    return {"Id": guid(1, n), "Name": name, "PolicyType": policy_type,
            "Applications": apps, "Description": "", "LinkedAgentPolicies": []}


def group_ref(g):
    return {"applicationType": 2, "description": "", "displayName": g["Name"], "id": g["Id"],
            "patterns": {}, "protectInstalledFiles": False, "restrictOpenSaveFileDialog": False}


def parent_process(g, all_ancestors=True):
    """PARENT_PROCESS pattern as it appears in exports: patterns.PARENT_PROCESS
    on a policy's Applications entry, pointing at an AppGroup by Id and Name."""
    return {"@type": "ParentProcess", "AllAncestors": all_ancestors,
            "ApplicationGroupId": g["Id"], "ApplicationGroupName": g["Name"], "isEmpty": False}


def with_parent(app, g, all_ancestors=True):
    """Copy of an app entry that only matches when launched by an app in group g."""
    app = dict(app, patterns=dict(app["patterns"]))
    app["patterns"]["PARENT_PROCESS"] = parent_process(g, all_ancestors)
    return app


def _self_entry(pid, internal_id, policy_type):
    return [{"Id": pid, "InternalId": internal_id, "PolicyType": policy_type}]


def win_policy(n, name, action, apps, internal_id, link_type=1):
    """Windows application policy (PolicyType 11). Action 1 Allow, 2 Block, 3 Elevate."""
    pid = guid(2, n)
    p = {"Id": pid, "Name": name, "PolicyType": 11, "Action": action}
    if action in (1, 3):
        p["AccessControl"] = None
    p["Applications"] = apps
    p.update({"Audit": True, "Description": "", "IsActive": False})
    if action == 2:
        p["KillRunningApps"] = True
    p["LinkedAgentPolicies"] = _self_entry(pid, internal_id, link_type)
    p["Priority"] = 40
    if action == 3:
        p.update({"RecordAuditVideo": False,
                  "UIAuditVideo": _ui("AuditVideoConfirmation"),
                  "UIAuditVideoError": _ui("AuditVideoLowDisk"),
                  "UIAuditVideoInit": _ui("AuditVideoNotify"),
                  "UIOnStart": _ui("Elevate"), "UIOnStartAdmin": _ui("Elevate")})
    elif action == 1:
        p.update({"RecordAuditVideo": False,
                  "UIAuditVideo": _ui("AuditVideoConfirmation"),
                  "UIAuditVideoError": _ui("AuditVideoLowDisk"),
                  "UIAuditVideoInit": _ui("AuditVideoNotify"),
                  "UIOnStart": _ui("StartAlert"), "UIOnStartAdmin": _ui("StartAlert"),
                  "UIReplaceUAC": _ui("StartAlert"), "UIReplaceUacAdmin": _ui("StartAlert")})
    else:
        p.update({"UIOnBlock": _ui("Block"), "UIOnStop": _ui("KillApp")})
    return p


def mac_block_policy(n, name, apps, internal_id):
    """macOS Block policy (PolicyType 13, self-entry PolicyType 1003)."""
    pid = guid(2, n)
    return {
        "Id": pid, "Name": name, "PolicyType": 13, "Action": 2, "Accounts": [],
        "Activation": {"ActivateDate": None, "AutoDelete": False, "DeactivateDate": None,
                       "Scheduler": None},
        "Applications": apps, "Audit": True, "ConditionalEnforcement": [], "Description": "",
        "ExcludeADComputerGroups": [],
        "ExcludeAccounts": {"CollectionId": ZERO, "CollectionName": None, "Operator": 0,
                            "SelectedAccountCollection": None, "UserGroupCollection": None},
        "ExcludeComputerGroups": [], "Executors": [], "IncludeADComputerGroups": [],
        "IncludeComputerGroups": [], "IsActive": False, "IsAppliedToAllComputers": True,
        "LinkedAgentPolicies": _self_entry(pid, internal_id, 1003), "Priority": 30,
        "UIOnBlock": _ui("MacBlock"),
    }


def other_policy_without_apps(n, name, internal_id):
    """Non-application policy (PolicyType 53, Action 25) with no Applications key."""
    pid = guid(2, n)
    return {"Id": pid, "Name": name, "PolicyType": 53, "Action": 25, "Audit": True,
            "Description": "", "Executors": [], "IsActive": False,
            "IsAppliedToAllComputers": False,
            "LinkedAgentPolicies": _self_entry(pid, internal_id, 1901), "RiskLevel": 2}


def document(policies, groups, end_user_uis=None):
    return {"Policies": policies, "AppGroups": groups, "TrustSoftwareDistributors": [],
            "UserAccessTokens": [], "EndUserUIs": end_user_uis}


def oversized_example():
    """The standard test input: one of each case the splitter must handle."""
    big = group(1, "Test Group Big", [win_app(i) for i in range(1, 1201)])
    small = group(2, "Test Group Small", [win_app(i) for i in range(1201, 1206)])
    policies = [
        win_policy(1, "Test Policy Uses Big Group", 3,
                   [group_ref(big)] + [win_app(i) for i in range(1206, 1208)], 90000001),
        win_policy(2, "Test Policy Big", 1,
                   [win_app(i) for i in range(2001, 3001)], 90000002),
        mac_block_policy(3, "Test Mac Policy Big", [mac_app(i) for i in range(1, 1001)], 90000003),
        win_policy(4, "Test Policy Small", 2,
                   [group_ref(small)] + [win_app(i) for i in range(3001, 3004)], 90000004),
        win_policy(5, "Test Policy Parent Process", 2,
                   [with_parent(win_app(i), big) for i in range(3101, 3104)]
                   + [win_app(3104)], 90000005),
    ]
    return document(policies, [big, small])
