# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from openjd.sessions._os_checker import is_windows

if is_windows():
    import win32security

MODIFY_READ_WRITE_MASK = 0x1301FF
FULL_CONTROL_MASK = 0x1F01FF


def get_aces_for_object(object_path: str) -> dict[str, tuple[list[int], list[int]]]:
    """Obtain a dictionary representation of the Access Control Entities (ACEs) of the
    given object. The returned dictionary has the form:
    {
        <username>: (
            [ <access masks>, ... ],
            [ <denied masks>, ... ]
        ),
        ...
    }
    """
    return_dict = dict[str, tuple[list[int], list[int]]]()
    sd = win32security.GetFileSecurity(object_path, win32security.DACL_SECURITY_INFORMATION)

    dacl = sd.GetSecurityDescriptorDacl()

    for i in range(dacl.GetAceCount()):
        ace = dacl.GetAce(i)

        ace_type = ace[0][0]
        access_mask = ace[1]
        ace_principal_sid = ace[2]

        account_name, _, _ = win32security.LookupAccountSid(None, ace_principal_sid)
        if account_name not in return_dict:
            return_dict[account_name] = (list[int](), list[int]())
        if ace_type == win32security.ACCESS_ALLOWED_ACE_TYPE:
            return_dict[account_name][0].append(access_mask)
        elif ace_type == win32security.ACCESS_DENIED_ACE_TYPE:
            return_dict[account_name][1].append(access_mask)

    return return_dict


def get_aces_for_principal_on_object(object_path: str, principal_name: str):
    """
    Returns a list of access allowed masks and a list of access denied masks for a principal's permissions on an object.
    Access masks for principals other than that specified by principal_name will be excluded from both lists.

    Arguments:
        object_path (str): The path to the object for which the ACE masks will be retrieved.
        principal_name (str): The name of the principal to filter for.

    Returns:
        access_allowed_masks (List[int]): All masks allowing principal_name access to the file.
        access_denied_masks (List[int]): All masks denying principal_name access to the file.
    """
    sd = win32security.GetFileSecurity(object_path, win32security.DACL_SECURITY_INFORMATION)

    dacl = sd.GetSecurityDescriptorDacl()

    principal_to_check_sid, _, _ = win32security.LookupAccountName(None, principal_name)

    access_allowed_masks = []
    access_denied_masks = []

    for i in range(dacl.GetAceCount()):
        ace = dacl.GetAce(i)

        ace_type = ace[0][0]
        access_mask = ace[1]
        ace_principal_sid = ace[2]

        if ace_principal_sid == principal_to_check_sid:
            if ace_type == win32security.ACCESS_ALLOWED_ACE_TYPE:
                access_allowed_masks.append(access_mask)
            elif ace_type == win32security.ACCESS_DENIED_ACE_TYPE:
                access_denied_masks.append(access_mask)

    return access_allowed_masks, access_denied_masks


def principal_has_access_to_object(object_path, principal_name, access_mask):
    access_allowed_masks, access_denied_masks = get_aces_for_principal_on_object(
        object_path, principal_name
    )

    return access_allowed_masks == [access_mask] and len(access_denied_masks) == 0
