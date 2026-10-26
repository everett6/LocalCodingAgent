/*---------------------------------------------------------------------------------------------
 *  Copyright (c) Microsoft Corporation. All rights reserved.
 *  Fork modifications Copyright (c) 2026 Local Coder contributors.
 *  Licensed under the MIT License. See License.txt in the project root for license information.
 *--------------------------------------------------------------------------------------------*/

import { Event } from '../../../../base/common/event.js';
import { InstantiationType, registerSingleton } from '../../../../platform/instantiation/common/extensions.js';
import { IOnboardingService } from '../common/onboardingService.js';

// Code OSS 1.143's vendor welcome wizard asserts defaultChatAgent at module load.
// Local Coder provides native provider setup in its bundled extension instead.
class LocalCoderOnboardingService implements IOnboardingService {
	declare readonly _serviceBrand: undefined;
	readonly onDidDismiss = Event.None;
	show(): void { /* Provider setup is exposed in the Local Coder activity bar. */ }
}

registerSingleton(IOnboardingService, LocalCoderOnboardingService, InstantiationType.Delayed);
