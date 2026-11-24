# Functional Document: XATA TODO Management Platform

## Mandatory requirement coverage

| ID | Priority | Described | Acceptance criterion |
|---|---|---|---|
| BR-001 | Must | yes | yes |
| BR-002 | Must | yes | yes |
| BR-003 | Must | yes | yes |
| BR-004 | Must | yes | yes |
| BR-005 | Must | yes | yes |
| BR-006 | Must | yes | yes |
| BR-007 | Must | yes | yes |
| BR-008 | Must | yes | yes |
| BR-009 | Should | yes | yes |
| BR-010 | Must | yes | yes |
| BR-011 | Must | yes | yes |
| BR-012 | Must | yes | yes |
| BR-013 | Should | yes | yes |
| BR-014 | Must | yes | yes |
| BR-015 | Must | yes | yes |

## Requirements

- **BR-001**: The system must allow an authenticated user to create a TODO. The create flow captures title, description, priority and due date, with optional assignment to a user, and then saves the TODO. [XATA_TODO_BRD-008-A] [XATA_TODO_BRD-009] [XATA_TODO_BRD-005]
- **BR-002**: Each TODO must contain title, description, status, priority and due date. Optional notes can also be added (in scope). A title is mandatory. [XATA_TODO_BRD-008-A] [XATA_TODO_BRD-005] [XATA_TODO_BRD-012]
- **BR-003**: Users must be able to edit TODOs they own or are authorized to manage. Editable information includes details, status, priority and due date, and reassignment where authorized. [XATA_TODO_BRD-008-A] [XATA_TODO_BRD-010]
- **BR-004**: Users must be able to mark TODOs as completed. A completed TODO must have Completed status. [XATA_TODO_BRD-008-A] [XATA_TODO_BRD-012] [XATA_TODO_BRD-005]
- **BR-005**: Users must be able to assign a TODO to another authorized user, at creation (optional) or by reassignment where authorized. Team Leads can create and assign TODOs. [XATA_TODO_BRD-008-A] [XATA_TODO_BRD-009] [XATA_TODO_BRD-010] [XATA_TODO_BRD-007]
- **BR-006**: The system must support exactly the statuses Open, In Progress and Completed for TODOs. [XATA_TODO_BRD-008-A]
- **BR-007**: The system must support the priorities Low, Medium, High and Critical for TODOs. [XATA_TODO_BRD-008-A]
- **BR-008**: Users must be able to filter TODOs by status, priority, assignee and due date. [XATA_TODO_BRD-008-B] [XATA_TODO_BRD-005]
- **BR-009**: Users should be able to search TODOs by title or description. [XATA_TODO_BRD-008-B] [XATA_TODO_BRD-005]
- **BR-010**: The dashboard must show Open, In Progress, Completed and Overdue TODO counts. The dashboard section also lists High/Critical priority TODOs. Overdue means the due date has passed and status is not Completed. [XATA_TODO_BRD-008-B] [XATA_TODO_BRD-011] [XATA_TODO_BRD-012]
- **BR-011**: Team Leads must be able to view TODOs assigned to their team members (basic team view). This supports monitoring overdue work. [XATA_TODO_BRD-008-B] [XATA_TODO_BRD-007] [XATA_TODO_BRD-005]
- **BR-012**: The system must maintain created-by, created-date, updated-by and updated-date for TODOs. [XATA_TODO_BRD-008-B] [XATA_TODO_BRD-005]
- **BR-013**: Users should be able to delete TODOs where permitted. Deleting a TODO must not delete the user account or other TODOs. [XATA_TODO_BRD-008-B] [XATA_TODO_BRD-010] [XATA_TODO_BRD-012]
- **BR-014**: The system must prevent unauthorized modification of another user's TODO. Only authorized users can assign or modify TODOs. [XATA_TODO_BRD-008-B] [XATA_TODO_BRD-012] [XATA_TODO_BRD-014]
- **BR-015**: The system must provide a web interface through supported browsers. The first release is web-based only, and there are no native mobile apps. [XATA_TODO_BRD-008-B] [XATA_TODO_BRD-013] [XATA_TODO_BRD-006]

## Business rules

- A TODO must have a title. [XATA_TODO_BRD-012]
- A due date cannot be earlier than the creation date unless explicitly permitted. [XATA_TODO_BRD-012]
- Completed TODOs must have Completed status. [XATA_TODO_BRD-012]
- Only authorized users can assign or modify TODOs. [XATA_TODO_BRD-012]
- Overdue means the due date has passed and the status is not Completed. [XATA_TODO_BRD-012]
- Deleting a TODO must not delete the user account or other TODOs. [XATA_TODO_BRD-012]

## Workflows

### Create TODO [XATA_TODO_BRD-009] [XATA_TODO_BRD-008-A]

1. Authenticated user opens the create TODO form
2. Enter title
3. Enter description
4. Select priority
5. Select due date
6. Optionally assign to a user
7. Save the TODO

### Manage TODO [XATA_TODO_BRD-010]

1. View details
2. Edit information
3. Change status
4. Change priority or due date
5. Reassign where authorized
6. Delete where authorized

### View dashboard [XATA_TODO_BRD-011] [XATA_TODO_BRD-008-B]

1. User opens the dashboard
2. System shows total open TODOs
3. System shows in-progress TODOs
4. System shows completed TODOs
5. System shows overdue TODOs
6. System shows High/Critical priority TODOs

## Acceptance criteria

- **BR-001, BR-002** [XATA_TODO_BRD-008-A] [XATA_TODO_BRD-009] [XATA_TODO_BRD-014]
  - Given An authenticated user is on the create TODO form
  - When They enter a title, description, priority and due date and save
  - Then A TODO is created containing title, description, status, priority and due date
- **BR-002** [XATA_TODO_BRD-012]
  - Given A user is creating a TODO
  - When They try to save without a title
  - Then The system does not save the TODO because a title is required
- **BR-003** [XATA_TODO_BRD-008-A] [XATA_TODO_BRD-014]
  - Given A user owns a TODO or is authorized to manage it
  - When They edit its information and save
  - Then The changes are persisted
- **BR-004** [XATA_TODO_BRD-008-A] [XATA_TODO_BRD-012]
  - Given A user can manage an Open or In Progress TODO
  - When They mark it as completed
  - Then Its status becomes Completed
- **BR-005** [XATA_TODO_BRD-008-A] [XATA_TODO_BRD-014]
  - Given A user is authorized to assign TODOs
  - When They assign a TODO to another authorized user
  - Then The TODO shows that user as assignee
- **BR-006** [XATA_TODO_BRD-008-A]
  - Given A user creates or edits a TODO
  - When They select a status
  - Then The options available are Open, In Progress and Completed
- **BR-007** [XATA_TODO_BRD-008-A]
  - Given A user creates or edits a TODO
  - When They select a priority
  - Then The options available are Low, Medium, High and Critical
- **BR-008** [XATA_TODO_BRD-008-B] [XATA_TODO_BRD-014]
  - Given Several TODOs exist with differing status, priority, assignee and due date
  - When A user applies a filter on any of these fields
  - Then Only the matching TODOs are returned
- **BR-010** [XATA_TODO_BRD-008-B] [XATA_TODO_BRD-012] [XATA_TODO_BRD-014]
  - Given TODOs exist in various states, including some past their due date and not Completed
  - When A user views the dashboard
  - Then Open, In Progress, Completed and Overdue counts correctly reflect the TODO state
- **BR-011** [XATA_TODO_BRD-008-B] [XATA_TODO_BRD-007]
  - Given A Team Lead has team members with assigned TODOs
  - When The Team Lead opens the team view
  - Then The TODOs assigned to their team members are shown
- **BR-012** [XATA_TODO_BRD-008-B]
  - Given A TODO is created and later updated by different users
  - When The audit information is viewed
  - Then Created-by, created-date, updated-by and updated-date reflect the actions performed
- **BR-014** [XATA_TODO_BRD-008-B] [XATA_TODO_BRD-012] [XATA_TODO_BRD-014]
  - Given A TODO belongs to another user and the current user is not authorized to manage it
  - When The user attempts to modify, assign or delete it
  - Then The system prevents the modification
- **BR-015** [XATA_TODO_BRD-008-B] [XATA_TODO_BRD-013]
  - Given A user has a supported browser
  - When They access the application
  - Then The web interface is available and the core TODO functions can be used
- **BR-009** [XATA_TODO_BRD-008-B] [XATA_TODO_BRD-014]
  - Given TODOs exist with various titles and descriptions
  - When A user searches for a term
  - Then TODOs whose title or description match are returned
- **BR-013** [XATA_TODO_BRD-008-B] [XATA_TODO_BRD-012]
  - Given A user is permitted to delete a TODO
  - When They delete it
  - Then The TODO is removed, and the user account and other TODOs are unaffected

## Open questions

- What does 'authorized to manage' mean for each role (Employee, Team Lead, Administrator)? Who may edit, assign, reassign or delete which TODOs? — *BR-003, BR-005, BR-013 and BR-014 depend on an authorization model that the BRD does not define.* [XATA_TODO_BRD-007] [XATA_TODO_BRD-008-A] [XATA_TODO_BRD-012]
- Who counts as an 'authorized user' to be assigned a TODO? Can an Employee assign TODOs to others, or only Team Leads? — *The roles table gives assignment only to Team Leads, while BR-005 says 'users'.* [XATA_TODO_BRD-007] [XATA_TODO_BRD-008-A]
- When may a due date earlier than the creation date be 'explicitly permitted', and who grants that permission? — *The business rule has an undefined exception that affects validation.* [XATA_TODO_BRD-012]
- Is a High/Critical priority count a required dashboard item? BR-010 lists four counts, but section 7.3 adds High/Critical priority TODOs. — *The dashboard scope differs between BR-010 and section 7.3.* [XATA_TODO_BRD-008-B] [XATA_TODO_BRD-011]
- Should deletion be hard or soft, and should it be recoverable? Is a TODO's history retained for audit? — *BR-013 and BR-012 do not specify delete behaviour or audit retention.* [XATA_TODO_BRD-008-B] [XATA_TODO_BRD-012]
- What are the supported browsers and the agreed NFRs (performance, security, availability)? How do users authenticate (for example SSO)? — *BR-015 and the acceptance criterion 'meets agreed NFRs' cannot be verified without them. The BRD also does not give a login method.* [XATA_TODO_BRD-008-B] [XATA_TODO_BRD-014] [XATA_TODO_BRD-013]
- How is team membership defined and maintained, for example whether a user can belong to several teams, and what scope a TODO's 'team' has? — *BR-011 depends on the team structure, which XATA is to supply but which is not specified.* [XATA_TODO_BRD-013] [XATA_TODO_BRD-008-B]
- Should Completed status be reversible, and how are status changes handled (for example reopening a completed TODO)? Is a TODO without an assignee allowed, and to whom does it belong? — *The status workflow and the unassigned case are not specified.* [XATA_TODO_BRD-008-A] [XATA_TODO_BRD-009] [XATA_TODO_BRD-012]
- What are the Administrator's 'basic platform configuration' and user-management functions? There is no matching business requirement. — *The role is described, but no BR covers user management or configuration. The scope also lists a 'basic user profile' without detail.* [XATA_TODO_BRD-007] [XATA_TODO_BRD-005]
- The final acceptance criterion says the core lifecycle must be completed without administrator assistance. Which actions count as the core lifecycle? — *The testable scope of this criterion is ambiguous.* [XATA_TODO_BRD-014]

## Assumptions

- XATA will provide the user list and team structure. [XATA_TODO_BRD-013]
- XATA will provide branding requirements. [XATA_TODO_BRD-013]
- The initial release is web-based. [XATA_TODO_BRD-013]
- XBS will propose the technical architecture based on the BRD and NFR. [XATA_TODO_BRD-013] [XATA_TODO_BRD-016]
- The first release will use a single enterprise environment. [XATA_TODO_BRD-013]
